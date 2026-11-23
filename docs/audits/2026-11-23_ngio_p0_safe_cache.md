# NGIO / Grass Cache — reconciliación adversarial de Fase 1 y P0 SAFE CACHE (Fase 2)

> **Fecha:** 2026-11-23 · **Base:** `origin/main` `9632a465f8520010dfbe89da4a29dd29f24875da`
> **Rama:** `arena/9a1eb8ae-sky-claw` · **Autor:** agente (Arena), a pedido del propietario.
> **Estado:** `P0_CHANGES_REQUESTED` **atendido**; sigue `CODE_READY_FOR_HUMAN_SMOKE`, **no**
> `MVP_CERTIFIED`. La §7 registra la revisión del propietario y qué se corrigió en cada punto.

Este documento reconcilia contra el código de la HEAD los hallazgos F-01…F-12 de la
auditoría consolidada del 23-11-2026 y registra la remediación del único `P0`.
No repite la auditoría: cada fila cita el código o la fuente que la sostiene.

## 1. Verificación de la fuente primaria (F-01)

**No se tomó el mecanismo de ningún informe.** Se leyó el código de NGIO-NG en el tag
`1.0.13` (API de contenidos de GitHub, 2026-11-23):

`src/GidFileCache.cpp::GidFileGenerationTask::Init()`

```cpp
auto fs = std::ifstream(Util::getProgressFilePath());
std::string l;
while (std::getline(fs, l)) {
    l = Util::StringHelpers::trim(l);
    if (l.empty()) continue;
    ProgressDone->insert(l);
}
IsResuming = !ProgressDone->empty();
...
if (ProgressDone->empty()) {
    auto dir = std::filesystem::path("Data/Grass");
    for (const auto& entry : std::filesystem::directory_iterator(dir)) {
        if (entry.path().extension() == ".dgid" || entry.path().extension() == ".cgid"
            || entry.path().extension() == ".fail") {
            remove(entry.path());
        }
    }
}
```

Y `GidFileGenerationTask::End()` borra el archivo de progreso al terminar — de ahí que
su ausencia sea la señal de "terminó" que usaba el runner.

**Consecuencias que el código de Sky-Claw violaba:**

1. El disparador del borrado es el flag **vacío**, no sólo ausente. `run()` escribía
   `""` en `PrecacheGrass.txt` en *todas* las invocaciones.
2. Cualquier línea no blanca preserva el caché y hace que NGIO reanude. La definición
   de "progreso" del contrato nuevo (contenido no blanco) coincide **exactamente** con
   la del binario (`trim` + descarte de líneas vacías).
3. `releases/latest` del repo de NGIO-NG devuelve `1.0.13` aunque existen tags
   `1.0.17`/`1.0.17b3`: el instalador queda anclado a una release que no es la última
   (F-06 confirmado, ver §3).

**NO VERIFICADO (sigue abierto):** el binario de Nexus #42161 2.2.0 no fue inspeccionado
(no hay acceso al ZIP ni rig Windows). El contrato nuevo es fail-closed respecto de las
dos versiones: si 2.2.0 conserva la semántica, Sky-Claw ya no destruye; si la cambió,
Sky-Claw sigue sin destruir. Lo que **no** se puede afirmar es compatibilidad de
instalación con 2.2.0.

## 2. Reconciliación F-01…F-12 contra la HEAD

| # | Veredicto propio | Evidencia en la HEAD | Acción en este paquete |
|---|---|---|---|
| F-01 | **CONFIRMADO** | `grass_cache_runner.py:249` escribía el flag vacío sin condición; fuente primaria §1 | **Corregido** (P0) |
| F-02 | **CONFIRMADO** | `_salida_exito` sólo exige `flag ausente + count>0 + bytes>0`; no mira `.fail`, ni procedencia, ni frescura; `etapas_previas_lods.py:77` sólo busca "algún `.cgid` no vacío" | Fuera de alcance (P1) |
| F-03 | **CONFIRMADO** | `xedit/runner.py:830,904,1238` pasan `-D:<game>/Data` y spawnean directo (`run_capture`), sin USVFS (`output_targets.py` lo documenta: ningún runner hereda la VFS) | Fuera de alcance (P2) |
| F-04 | **CONFIRMADO** | `GrassCacheService.generate()` nunca llama a `analyze_prerequisites`; no hay testigo vinculado al perfil | Fuera de alcance (P2) |
| F-05 | **CONFIRMADO** | `_vigilar()` sólo detecta cancelación/deadline/disco/muerte; la racha de stall se calcula post-crash ⇒ un freeze con PID vivo consume hasta `max_runtime_s` (12 h) | Fuera de alcance (P1) |
| F-06 | **CONFIRMADO** | `tools_installer.py:266` (`releases/latest` de NGIO-NG) + selector por nombre `ngio-ae` (L1215); `releases/latest` = 1.0.13 mientras hay tags 1.0.17 | Fuera de alcance (P3) |
| F-07 | **CONFIRMADO en escritura / P-RIESGO en efecto** | `grass_profile.py:85-92` escribe sólo 3 claves de `[GrassConfig]`; MO2 superpone el archivo entero | Fuera de alcance (P3) |
| F-08 | **CONFIRMADO** | `vfs.py:589` usa `moshortcut://SKSE` sin atestar el nombre efectivo de la entrada; `ensure_ngio` no tiene gate de SKSE | Fuera de alcance (P3) |
| F-09 | **CONFIRMADO** | `output_targets.py` no conoce ningún destino de grass (grep sin resultados); no hay productor de rollback | Fuera de alcance (P4) |
| F-10 | **CONFIRMADO** | Las strategies se registran en `tool_dispatcher.py:331,338`, pero `app/agent/tools/__init__.py` y `discovery/registry.py` no mencionan grass: no hay entrada en el registro del agente ni botón de GUI | Fuera de alcance (P5) |
| F-11 | **REFUTADO para symlinks; P-RIESGO residual en junctions** | `grass_profile.py` usa `link_kind_and_identity_or_raise_with_retry` y `_reject_link_or_reparse` antes de copiar (L132-223) | Sin cambios |
| F-12 | **CONFIRMADO** | `pipeline.grass_cache.*` no tiene **ningún** suscriptor en el árbol (grep vacío); los `.fail` y los logs de NGIO no se inspeccionan | Parcial (el resultado ahora expone `mode` y `backup_dir`) |

**Correcciones a la auditoría consolidada que este trabajo mantiene:**

- Grass Cache Helper NG (SKSE) ≠ plugin "Precache Grass" de MO2. No se retiró nada.
- La desaparición del INI temporal no prueba nada sobre el consumo del caché: no se tocó
  ese punto y sigue **NO VERIFICADO**.
- `count(.cgid) == count(celdas)` no se usa como criterio en ninguna parte.

## 3. P0 — SAFE CACHE: qué cambió

Módulo nuevo `sky_claw/local/tools/grass_cache_safety.py` (mypy estricto) que contesta
**una sola vez** "¿puedo arrancar?" y es la única pieza que escribe el flag:

| Modo | Precondición | Acción sobre el flag |
|---|---|---|
| `new` (default) | sin `.cgid/.dgid/.fail` **y** sin progreso | escribe vacío |
| `resume` | flag con progreso **o** progreso archivado con huella coincidente | **no lo toca** (o lo restaura byte a byte) |
| `rebuild` | `REBUILD` **+** reconocimiento de cobertura USVFS **+** respaldo verificado por **contenido** contra el caché actual | archiva el progreso y escribe vacío |

Propiedades que quedan ancladas por tests:

- Un run `new` con caché previo **no lanza**: `cache_existente_sin_decision`.
- Un flag con progreso **nunca se trunca** sin decisión; `resume` lo conserva byte a byte.
- `rebuild` sin aprobación, sin raíz de estado o sin respaldo que cubra el inventario
  actual **no arranca**.
- El respaldo se verifica **por contenido en los dos lados**: lo que el manifiesto declara
  tiene que coincidir con el origen *actual* y con los bytes *actuales* del destino. Un
  respaldo de un caché con los mismos nombres y tamaños pero distinto contenido (o alterado
  en el destino conservando el tamaño) **no se reusa**: se aparta con nombre explícito y se
  recopia. Antes de escribir el flag vacío se repite la verificación contra el estado
  vigente (`verificar_respaldo_para_borrado`).
- Los enlaces (symlink/junction) en el caché, en el flag **o en cualquier componente de la
  ruta del caché** son `HOLD` fail-closed: se reutiliza `exigir_contencion_fisica` del repo
  (lstat por componente, junctions de Windows incluidos).
- Al salir, el flag se **neutraliza** (el juego del usuario no puede quedar en modo
  precache) pero su contenido se archiva **byte a byte** (base64 + sha256, sin decodificar
  como texto) atado a la huella del caché, y `resume` sólo lo restaura si la huella y el
  sha256 del archivo archivado coinciden. Un `unlink` que falla **no** se reporta como
  limpieza exitosa: el resultado del run expone `flag_residual` y los errores concretos, y
  el servicio los journaliza por la misma vía que el teardown incompleto.
- El servicio corre el mismo `preparar_arranque` **antes de clonar el perfil**, y el
  runner lo repite sobre el inventario fresco. Un rechazo no crea transacción de journal.

## 4. Límites declarados (no son verdes)

1. **Nada de esto se probó en Windows con MO2, USVFS, SKSE ni NGIO real.** No hay rig
   en este entorno. Por eso el veredicto es `CODE_READY_FOR_HUMAN_SMOKE`.
2. **NGIO 2.2.0 (Nexus) no fue inspeccionado.** El contrato no depende de su semántica.
3. **Reanudación tras muerte dura (SIGKILL/corte de luz):** el `finally` no corre, así que
   no hay archivo de progreso archivado y el barrido de arranque (U-03) borra el flag
   huérfano. En ese escenario `resume` se niega y el camino es `rebuild`, que **respalda
   antes de borrar** — el caché físico de `overwrite/Grass` se conserva, pero se regenera y
   sigue vigente el HOLD de USVFS. Cerrarlo exige que el barrido archive antes de borrar (P1).
4. **El respaldo copia, no mueve**, y vive en `SystemPaths.runtime_state_dir()`. No se
   verifica que esté en otro volumen: "independiente" es aquí lógico (fuera del árbol
   MO2), no físico.
5. **Los `.fail` se respaldan y se inventarían, pero no se interpretan** (F-02).
6. Un `rebuild` sobre un caché de varios GB duplica ese espacio mientras dura el run, y la
   verificación de contenido **lee los dos lados completos** (dos pasadas sha256 sobre el
   origen y una sobre el destino): en un caché de GB eso es tiempo de E/S real, no un
   chequeo instantáneo. Es el costo de que el respaldo signifique algo; se paga sólo en
   `rebuild`.
7. **El rechazo de enlaces es conservador por diseño:** un rig cuyo `overwrite/` cuelgue de
   un junction legítimo (bibliotecas movidas, perfiles redirigidos) se rechaza con
   `cache_raiz_enlazada` en vez de operar sobre él. No hay escape manual en P0; el operador
   tiene que apuntar el tool a la ruta real. Relajarlo exige decidir *qué* junction es
   confiable, que es justamente lo que no se puede demostrar sin rig.
8. **`PrecacheGrass.txt` puede no ser el archivo efectivo:** `Util::getProgressFilePath()`
   de NGIO 1.0.13 tiene un fallback a `.txt.txt`. El módulo inspecciona el nombre sin
   fallback y falla cerrado respecto de lo que no ve (ver §1).

## 5. Gates ejecutados de la PRIMERA versión (2026-11-23, Linux, Python 3.11.2)

> Registro histórico del primer parche (`92472f9`). Los gates vigentes están en §7 y §9.

| Gate | Comando | Resultado |
|---|---|---|
| ROJO previo | `pytest tests/test_grass_cache_runner.py -k cache_existente_sin_decision` | `TimeoutError`: el runner **arrancaba** con caché previo |
| VERDE focalizado | `pytest tests/test_grass_cache_{runner,service,safety,strategy}.py` | **128 passed** |
| Ruff | `ruff check` + `ruff format --check` sobre el write-set | limpio |
| mypy | `mypy` sobre los módulos (nuevo módulo agregado a la lista estricta; verificado con una sonda de tipos que el gate realmente lo mira) | sin errores |
| Suite completa | `pytest` | 20434 passed, 626 skipped, **136 failed preexistentes** |
| Baseline | los mismos 136 node IDs sobre el árbol con `git stash` | **idénticos** (diff vacío ⇒ 0 regresiones) |

Los 136 fallos preexistentes son de `runtime_vault*`, `frozen_runtime*`, `journal`,
`telegram_sender` y `process_helper` (módulos Windows-only o dependientes de timing,
ejecutados en Linux). El módulo `tests/test_runtime_vault_operator_verifier.py` no
colecciona en POSIX (`_SECURITY_ATTRIBUTES`) y se excluyó por eso, también en el baseline.

## 6. Orden de los paquetes siguientes

`P1` veredicto + watchdog de no-progreso (depende de P0) → `P2` USVFS/Fase A → `P3`
instalador + INI → `P4` publicación en mod dedicado + rollback → `P5` superficies.
Antes de cada uno: `git fetch`, PRs abiertos (#1003, #1006, #1007 al cierre de este
documento) y write-sets reales — no se asume que sigan abiertos.


## 7. Revisión del propietario (`P0_CHANGES_REQUESTED`) — qué se corrigió

Dictamen del propietario sobre el primer parche: la arquitectura `NEW/RESUME/REBUILD` es
adecuada, pero la integridad del respaldo no estaba garantizada. Los cuatro puntos y la
precondición de certificación, con el cambio y el test que lo fija:

| # | Defecto reportado | Corrección | Test que lo reproduce |
|---|---|---|---|
| **B1** | `_respaldo_existente_verifica()` reusaba el respaldo comparando metadatos y tamaños: origen `AAAA`→`BBBB` (mismos 4 bytes) reusaba el respaldo viejo, y un destino alterado a `XXXX` con el mismo tamaño también pasaba | El reuso exige **tres igualdades por hash de contenido**: manifiesto == origen **actual**, destino **actual** == manifiesto, y conteos/bytes == inventario. Un respaldo que no verifica se **aparta** (`*.invalido-<ts>`) y se recopia; nunca se pisa ni se borra. Además, antes de escribir el flag vacío se repite la verificación contra el estado vigente (`verificar_respaldo_para_borrado`) | `test_respaldo_no_se_reutiliza_si_el_origen_cambio_con_el_mismo_tamano`, `test_respaldo_alterado_en_el_destino_no_se_reutiliza`, `test_verificacion_previa_al_borrado_rechaza_cache_cambiado`, `test_verificacion_previa_al_borrado_rechaza_respaldo_alterado` |
| **B2** | `inventariar_cache()` rechazaba enlaces en archivos individuales pero permitía que `grass_dir` (o un ancestro) fuera un symlink/junction a un árbol externo | `_validar_raiz_fisica()` reutiliza `exigir_contencion_fisica` del repo (lstat por componente, sin seguir enlaces, clasifica junctions de Windows) anclando en la raíz del volumen. Se aplica al inventariar y al respaldar; el respaldo además exige `exigir_arbol_copiable_sin_reparse` | `test_cache_raiz_enlazada_se_rechaza`, `test_componente_intermedio_enlazado_se_rechaza` |
| **C3** | `_leer_flag()` decodificaba con `errors="replace"`: archivar/restaurar podía cambiar los bytes originales | El contenido del flag viaja como **bytes** de punta a punta (`InventarioCache.flag_progreso`, `PlanArranque.contenido`), se archiva en base64 con sha256 y se restaura con `write_bytes`. "Tiene progreso" se decide con `splitlines()` + `strip()` sobre bytes, igual que NGIO (`trim` + descarte de blancas), sin decodificar | `test_flag_con_bytes_no_utf8_se_preserva_byte_a_byte`, `test_resume_restaura_bytes_no_utf8_sin_recodificar`, `test_progreso_archivado_corrupto_no_se_restaura` |
| **C4** | No se declaraba fallo si `PrecacheGrass.txt` no se podía eliminar | `neutralizar_flag()` devuelve `NeutralizacionFlag(borrado, archivado, errores)` y nunca propaga; se re-verifica la ausencia después del `unlink`; un enlace no se borra. El runner expone `flag_residual` + `limpieza_errores` y el servicio los journaliza como teardown incompleto. El `finally` sólo toca un flag que el run **adoptó** | `test_neutralizacion_reporta_fallo_de_borrado`, `test_neutralizacion_no_borra_un_flag_que_es_un_enlace`, `test_flag_residual_se_reporta_y_no_se_da_por_limpio`, `test_guard_que_se_niega_no_borra_el_flag_existente`, `test_flag_residual_llega_al_resultado_y_al_teardown` |
| **USVFS** | El inventario se limita a `overwrite/Grass`: no se demostró si NGIO puede borrar `.cgid` de otros mods activos | **HOLD explícito por defecto** para todo arranque que escriba el flag vacío —`new` y `rebuild`—: sin `ack_usvfs_no_verificado=True` no arranca (`usvfs_no_demostrado`) y **no se toca el disco** (la precondición corre antes de respaldar). El reconocimiento es visible en el prompt HITL y queda en el log. `plan.a_dict()` declara `cobertura_usvfs: no_verificada` | `test_rebuild_sin_ack_de_usvfs_queda_en_hold`, `test_rebuild_con_ack_explicito_deja_de_estar_en_hold`, `test_rebuild_sin_ack_de_usvfs_no_arranca`, `test_generate_rebuild_sin_ack_de_usvfs_queda_en_hold`, `test_describe_for_approval_avisa_que_el_respaldo_no_cubre_los_mods` |

### Determinación sobre USVFS (lo que sí y lo que no se puede afirmar)

- **Se puede afirmar** que el producto espera cachés en mods: `validators/etapas_previas_lods.py`
  busca `*.cgid` tanto en `overwrite/Grass` como en el `Grass/` de **cada mod habilitado** del
  `modlist.txt`. Si NGIO enumera `Data/Grass` bajo la VFS, ese `Grass/` de mod está en la
  enumeración.
- **No se puede afirmar** qué hace `remove()` sobre una ruta virtualizada (borrado físico vs.
  tombstone en `overwrite`) ni si la superposición incluye todos los mods del perfil. Sin rig
  Windows es `NO VERIFICADO`, así que **no se promete protección completa**: el respaldo se
  describe siempre como "lo que Sky-Claw puede ver", nunca como cobertura total.
- **No se amplió P0 a una refactorización de USVFS.** Cerrar el HOLD (inventariar el overlay y
  demostrar la semántica de borrado) es trabajo de P2, con evidencia de rig.

### Gates de esta revisión

| Gate | Resultado |
|---|---|
| Tests focalizados (runner + service + safety + strategy + profile + HITL + BDD) | **228 passed, 2 skipped**, exit 0 |
| `ruff check` / `ruff format --check` sobre `sky_claw/ scripts/ tests/` | limpio |
| `mypy sky_claw/ scripts/` | mismos 694 errores preexistentes (17 archivos, ninguno del write-set); **0** en los módulos de grass |
| Suite completa vs. baseline | ver §9 |

## 8. Segunda revisión independiente (`b11860f`) — qué se corrigió

La revisión aceptó la integridad SHA-256, la validación de raíz física y el progreso
byte-fiel, y rechazó el parche por seis defectos reproducidos. Todos se reprodujeron con
test antes de corregirlos (rojo), y el rojo de cada uno está descrito abajo:

| # | Defecto | Reproducción (rojo) | Corrección | Test |
|---|---|---|---|---|
| **B1** | El servicio devolvía `success=True` con `flag_residual=True` | el test exigía éxito con el flag en disco | `exito` exige limpieza completa; el resultado lleva `success=False`, `message` explicativo y `teardown_failures`; el journal cierra la TX como fallida | `test_flag_residual_llega_al_resultado_y_al_teardown`, `test_progreso_perdido_no_declara_exito` |
| **B2** | `aplicar_plan` escribía sobre `PrecacheGrass.txt.tmp`, nombre previsible: un symlink ahí redirigía la escritura fuera del directorio | el archivo víctima quedaba truncado a 0 bytes | temporal **impredecible** (`.<flag>.<hex>.tmp`), **exclusivo** (`O_CREAT\|O_EXCL\|O_NOFOLLOW`) en el mismo directorio, limpieza del temporal y **relectura del flag** para confirmar la sustitución | `test_aplicar_plan_no_escribe_a_traves_de_un_temporal_enlazado`, `test_aplicar_plan_rechaza_un_temporal_ya_existente`, `test_aplicar_plan_verifica_que_la_sustitucion_ocurrio` |
| **B3** | Si `link_kind_or_raise()` fallaba, se registraba el error **y se borraba igual** | el flag se borraba con la clasificación en error | fail-closed: sin clasificación no hay `unlink`; lo mismo si la lectura falla (enlace, directorio, permisos) | `test_neutralizacion_con_clasificacion_fallida_no_borra` |
| **B4** | Si fallaba `archivar_progreso`, el flag se borraba y se declaraba `limpio=True`; y el archivado previo a `rebuild` era `suppress(OSError)` | la evidencia de reanudación se perdía en silencio | `NeutralizacionFlag.progreso_perdido` (borrar sigue siendo obligatorio; declararlo limpio no), `limpio = borrado and not progreso_perdido`, propagado a `GrassCacheRunResult.progreso_perdido` y al journal; el archivado previo a `rebuild` **niega el arranque** (`progreso_no_archivado`) | `test_si_falla_el_archivado_no_se_declara_limpio`, `test_rebuild_no_pisa_el_progreso_si_no_puede_archivarlo`, `test_progreso_perdido_llega_al_resultado` |
| **B5** | El ack de USVFS sólo protegía `rebuild`; `new` también escribe el flag vacío, y el `remove()` de NGIO alcanza el overlay de mods que el inventario no ve | `new` corría sin reconocimiento | HOLD para **todo** arranque que escriba vacío (código único `usvfs_no_demostrado`), fail-fast antes de tocar disco en `rebuild`; el prompt HITL de `new` lo advierte | `test_new_sin_ack_de_usvfs_queda_en_hold`, `test_new_sin_ack_de_usvfs_no_arranca`, `test_generate_new_sin_ack_de_usvfs_queda_en_hold`, `test_describe_for_approval_advierte_cobertura_usvfs_en_new` |
| **B6** | La identidad del progreso era `(nombre, tamaño)`: un `.cgid` con otro contenido y el mismo tamaño permitía reanudar con el progreso de un caché distinto | el progreso se restauraba | `archivar_progreso` guarda `huella_contenido` (sha256 de nombre+tamaño+sha256) y exige `grass_dir`; `recuperar_progreso` compara el contenido actual y levanta `progreso_de_otro_cache` | `test_no_se_restaura_progreso_de_un_cache_con_mismo_tamano_y_otro_contenido` |

**Costo declarado de B6:** archivar el progreso ahora hashea el caché (una lectura completa,
sólo cuando al salir queda un flag con progreso — es decir, en runs cortados). La reanudación
por evidencia archivada también hashea el caché antes de restaurar. Es el precio de que la
evidencia signifique algo; no se paga en el camino feliz (NGIO borra el flag en `End()`).

**Efecto de B5 en el uso:** sin `ack_usvfs_no_verificado=True` **no se puede** correr un
precache nuevo. Es deliberado: el contrato ya no afirma que `new` sea inofensivo mientras el
borrado de NGIO pueda alcanzar `.cgid` de mods que Sky-Claw no inventaría.

## 9. Gates de la segunda revisión (2026-11-23, Linux, Python 3.11.2)

| Gate | Comando | Resultado |
|---|---|---|
| ROJO por defecto | los 6 defectos, test escrito antes del arreglo | **13 fallos con la causa correcta** (archivo víctima truncado, borrado tras clasificación fallida, `progreso_perdido` inexistente, `grass_dir` faltante, HOLD ausente en `new`) y **verde** después |
| VERDE focalizado | `pytest tests/test_grass_cache_{safety,runner,service,strategy}.py` | **160 passed** |
| VERDE focalizado ampliado | `+ tests/test_grass_profile.py tests/test_hitl_destructive_gate.py tests/bdd` | **228 passed, 2 skipped** |
| Ruff | `ruff check` + `ruff format --check` sobre `sky_claw/ scripts/ tests/` | *All checks passed!* / *1156 files already formatted* |
| mypy | `mypy sky_claw/ scripts/` | 694 errores preexistentes en 17 archivos (ninguno del write-set); **0** en los tres módulos de grass |
| Suite completa | `pytest -q --tb=no --ignore=tests/test_runtime_vault_operator_verifier.py` | **20466 passed, 626 skipped, 136 failed** (13 tests nuevos verdes) |
| Baseline | `git stash -u` + `pytest --lf` (mismo árbol, sin el parche) | **136 fallos, node IDs idénticos** (`diff` vacío ⇒ 0 regresiones) |

Los 136 fallos preexistentes son de `runtime_vault*`, `frozen_runtime*`, `journal`,
`telegram_sender` y `process_helper` (módulos Windows-only o dependientes de timing); ninguno
pertenece a grass, HITL, BDD ni al dispatcher.

## 10. Tercera revisión independiente (`73ec59e`) — qué se corrigió

| # | Defecto | Rojo reproducido | Corrección | Test |
|---|---|---|---|---|
| **B7** | `archivar_progreso` escribía en `<huella>.json.tmp` (nombre derivable): un symlink ahí sobrescribía un archivo ajeno. El manifiesto del respaldo usaba el mismo patrón (`.manifiesto.json.tmp`) | el archivo víctima quedaba con el JSON de evidencia | `_escribir_json_atomico()` escribe **todo** metadato con el temporal exclusivo del flag (`O_CREAT\|O_EXCL\|O_NOFOLLOW`, nombre impredecible, mismo directorio), reemplaza y **relee**. Se valida la contención física de la raíz de estado al archivar **y** al recuperar (`estado_raiz_enlazada`), y la del destino del respaldo (`respaldo_raiz_enlazada`) | `test_archivado_no_sobrescribe_victima_de_temporal_enlazado`, `test_no_quedan_temporales_de_nombre_predecible_en_el_modulo`, `test_raiz_de_estado_enlazada_se_rechaza` |
| **B8** | `aplicar_plan` retornaba sin más en `accion == "conservar"`: si el flag se vaciaba entre el preflight y el arranque, RESUME continuaba sin evidencia | el plan de `conservar` se aplicaba sobre un flag vacío | `_exigir_flag_conservado()` revalida presencia, tipo y **bytes exactos**; cualquier cambio falla cerrado (`flag_cambio_despues_del_preflight`) sin lanzar el juego y **sin tocar** lo que cambió otro | `test_resume_rechaza_flag_vaciado_despues_del_preflight`, `test_resume_rechaza_flag_borrado_despues_del_preflight` |
| **B9** | Si `os.replace()` escribía el flag y fallaba el postcheck, el runner no lo adoptaba y podía dejar un flag vacío sin limpiar | el flag quedaba en disco y `launch_game` no se llamaba | `EstadoAplicacion(escrito, verificado)` separa "la sustitución ocurrió" de "el contenido se verificó"; el runner adopta el flag en cuanto `escrito` es verdadero, también en la rama de error, así el `finally` lo neutraliza | `test_error_posterior_a_replace_no_deja_flag_vacio_sin_limpiar` |
| **Contrato** | `run()` podía devolver `success=True` con `limpio=False` (dependía del servicio) | un run `completed` con limpieza fallida se leía como éxito | `run()` declara `success=False` y un `message` con la causa cuando la limpieza no fue completa | `test_limpieza_incompleta_no_devuelve_exito` |

**USVFS — aceptación de riesgo, no certificación.** `ack_usvfs_no_verificado=True` no mide,
no enumera y no demuestra nada sobre la USVFS: sólo deja constancia de que quien lo activa
sabe que el `remove()` de NGIO puede alcanzar archivos que Sky-Claw no inventarió. El plan
declara siempre `cobertura_usvfs: no_verificada`. Consecuencia operativa: **no se ejecutan
operaciones destructivas sobre cachés reales** sin evidencia de rig o un respaldo completo de
los archivos alcanzables; el ack no reemplaza ninguna de las dos cosas.

### Gates de la tercera revisión

| Gate | Comando | Resultado |
|---|---|---|
| ROJO por defecto | B7/B8/B9 + invariante de `run()`, test escrito antes del arreglo | **7 fallos con la causa correcta** (víctima sobrescrita con el JSON de evidencia, `conservar` aplicado sobre un flag vacío, flag vacío sin limpiar tras el postcheck, `success=True` con limpieza incompleta) y **verde** después |
| VERDE focalizado | `pytest tests/test_grass_cache_{safety,runner,service,strategy}.py` | **167 passed** |
| VERDE focalizado ampliado | `+ tests/test_grass_profile.py tests/test_hitl_destructive_gate.py tests/bdd` | **235 passed, 2 skipped** |
| Ruff | `ruff check` + `ruff format --check` sobre `sky_claw/ scripts/ tests/` | *All checks passed!* / *1156 files already formatted* |
| mypy | `mypy sky_claw/ scripts/` | 694 errores preexistentes en 17 archivos (ninguno del write-set); **0** en los módulos de grass |
| Suite completa | `pytest -q --tb=no --ignore=tests/test_runtime_vault_operator_verifier.py` | **20473 passed, 626 skipped, 136 failed** (7 tests nuevos verdes) |
| Baseline | `git stash -u` + `pytest --lf` (mismo árbol sin el parche) | **136 fallos, node IDs idénticos** (`diff` vacío ⇒ 0 regresiones) |

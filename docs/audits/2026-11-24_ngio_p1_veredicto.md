# NGIO P1 — Veredicto por evidencia, integridad del resultado y watchdog

> **Fecha:** 2026-11-24/25 · **Base:** `origin/main` `a378b19b` (merge de `0c23c243` con
> `origin/main` en la rama P1) · **Rama:** `feat/ngio-p1-veredicto-watchdog` · **PR:** ver cuerpo del PR.
> **Estado:** `CODE_READY_FOR_INDEPENDENT_REVIEW`. **No** es `MVP_CERTIFIED` ni `PRODUCTION_READY`:
> nada de esto se midió en Windows con MO2/USVFS/SKSE/NGIO reales.
> **Diseño:** [`docs/design/specs/2026-11-24-ngio-p1-veredicto-concepts-brief.md`](../design/specs/2026-11-24-ngio-p1-veredicto-concepts-brief.md)

P1 cierra F-02 (falso éxito de generación), F-05 (proceso vivo sin progreso) y F-12 (diagnóstico
insuficiente) del audit de P0, **sin tocar `grass_cache_safety.py`**: las garantías NEW/RESUME/REBUILD,
el respaldo verificado y los seis invariantes de autorización quedan exactamente como estaban.

## 1. Decisiones del propietario y cómo se implementaron

| Decisión | Implementación | Ancla |
|---|---|---|
| **D1** — `success` conserva compatibilidad pero **no basta**: exige finalización operacional legítima; un caché preexistente sin evidencia no puede dar falso éxito; los consumidores miran el veredicto; sin recomendar REBUILD | `exito_operacional` = `outcome == completed` ∧ caché no vacío ∧ terminalidad demostrada ∧ limpieza completa ∧ **trabajo de esta corrida demostrado**. `success = exito_operacional`. `certificado` es un eje aparte y exige además cero `.fail` y todos los worldspaces con artefactos. El mensaje de no-certificación enumera razones y **no** menciona rebuild | `tests/test_grass_cache_verdict.py`, `test_p1_rebuild_sin_regeneracion_no_declara_exito`, `test_p1_limpieza_incompleta_degrada_el_veredicto_en_run` |
| **D2** (RD-12) — certificación limitada de RESUME con alcance explícito | `alcance_certificado ∈ {corrida_completa, trabajo_de_la_corrida, ninguno}` + `trabajo_de_la_corrida`, `integridad_observable` y `completitud_global: no_verificada`. RESUME certifica sólo como `trabajo_de_la_corrida`; sin evidencia de avance → `completed_unverified` con alcance `ninguno` | `test_resume_con_progreso_certifica_con_alcance_limitado`, `test_resume_sin_evidencia_de_avance_no_certifica_ni_da_exito`, `test_completitud_global_nunca_se_certifica_en_p1` |
| **D3** — implementar C/D/E/F preservando P0 | P0 intacto; TDD (rojo antes de cada pieza), 7 mutantes dirigidos, consumidores auditados | §4 y §5 de este documento |
| **D4** — autoinstalador en rig descartable | **No ejecutado**: no existe rig físico demostrado ni canal HITL que yo pueda aprobar. Se entrega informe aparte con el plan y lo verificado en lectura | [`2026-11-25_ngio_autoinstalador_rig.md`](2026-11-25_ngio_autoinstalador_rig.md) |

## 2. Qué cambió

| Archivo | Cambio |
|---|---|
| `sky_claw/local/tools/grass_cache_verdict.py` **(nuevo)** | Política única y **pura** (sin I/O, sin asyncio): `GrassCacheOutcome` (movido acá; el runner lo re-exporta), `GrassCacheVerdict`, `AlcanceCertificado`, `CacheObservado`, `EvidenciaVeredicto`, `VeredictoCache`, `evaluar_veredicto()`, `CODIGOS_DE_RAZON` (vocabulario cerrado) |
| `sky_claw/local/tools/grass_cache_runner.py` | Config (`worldspaces`, `progress_poll_interval_s`, `stall_progress_s`, `stall_grace_s`, `terminality_wait_s`) + validación; watchdog de progreso en `_vigilar`/`_vigilar_progreso`; muestreo barato (`_muestra_de_progreso_sync`) y lectura incremental del flag (`_leer_avance_del_flag`, `ws_`); `_verificar_terminalidad`/`_clasificar_terminalidad`; `_capturar_baseline_sync`; `_inventario_de_egreso_sync`; `_reunir_evidencia_de_egreso`; `_evidencia_a_dict`; `_veredicto_final` (dos fases) y `_mensaje_de_veredicto`; `_resultado` con veredicto; `GrassCacheRunResult` + 5 campos |
| `sky_claw/local/tools/grass_cache_service.py` | `worldspaces` al config; propagación de `verdict`/`certificado`/`verdict_razones`/`alcance_certificado`/`evidencia`/`limpieza_errores`; `_journalizar_veredicto` (operación dentro de la TX); payload del evento con veredicto |
| `sky_claw/app/db/journal_contracts.py` | `GRASS_VERDICT_KIND` + `is_grass_verdict_unverified` (fuente única de los `kind`, como los otros discriminadores) |
| `pyproject.toml` | `grass_cache_verdict` a la lista **estricta** de mypy (`sky_claw.local.tools.*` está exento) |
| `tests/test_grass_cache_verdict.py` **(nuevo)** | 36 tests: matriz completa, vocabulario congelado, D1/D2, dos fases, terminalidad, worldspaces |
| `tests/test_grass_cache_runner.py` | +11 tests P1: procedencia, `.fail` atribuible/histórico, worldspaces, limpieza, watchdog (corte, no-corte, gracia), terminalidad (unknown y espera) |
| `tests/test_grass_cache_service.py` | +5 tests P1: propagación sin pérdida, no reinterpretación, journal certificado/no certificado, evento |
| `tests/test_grass_cache_strategy.py` | +1: la strategy no reinterpreta el veredicto |
| `tests/bdd/support/grass_cache_harness.py` | El doble ahora modela un run **certificado** (verdict + alcance): `success`/`message` son consecuencia de la evidencia, no una constante del mock |
| `docs/design/specs/2026-11-24-…-concepts-brief.md` | Brief con D1/D2 reconciliadas, revisión adversarial (13 dudas, 3 defectos propios encontrados) y write-set |

**Un solo test existente cambió de contrato**, y se justifica en el propio test:
`test_flag_borrado_justo_antes_del_crash_no_relanza` afirmaba `success is True` sobre un REBUILD con
caché preexistente que la corrida **no tocó**. Ese assert congelaba exactamente el falso éxito de F-02.

## 3. Evidencia RED → GREEN

### 3.1 Rojo medido ANTES de implementar (FASE B)

Cinco tests de reproducción (archivo de trabajo, ya retirado) fallaban con la causa correcta:

| Caso | Rojo observado |
|---|---|
| caché preexistente sin evidencia | `AttributeError: 'GrassCacheRunResult' object has no attribute 'verdict'` |
| `.fail` histórico / nuevo | ídem |
| PID vivo sin progreso | `AssertionError: assert 'timeout' == 'stalled'` |
| worldspaces en el resultado | `TypeError: GrassCacheConfig.__init__() got an unexpected keyword argument 'worldspaces'` |

Y una sonda temporal registró el comportamiento de `main` en esos escenarios:

| Escenario | Antes (`main`) | Después (P1) |
|---|---|---|
| `rebuild` sobre `.cgid` preexistente, sin escribir nada | `outcome=completed`, **`success=True`**, `message=''` | `completed_unverified`, `success=False`, `archivos_heredados_sin_procedencia` |
| ídem + `.fail` con contador 2 | `outcome=completed`, **`success=True`**, `message=''` | `completed_unverified`, `fallos_de_celda_historicos` |
| juego inmortal sin progreso | `outcome=timeout`, `elapsed_s=0.609/0.6`, `stalled=False` | `outcome=stalled`, `elapsed_s < max_runtime_s`, `progreso_detenido_s` en la evidencia |

### 3.2 Verde

| Corrida | Resultado |
|---|---|
| Núcleo P1 (`verdict` + `runner` + `service` + `strategy`) | **345 passed** |
| Focalizados + anclas (`safety`, `profile`, `bdd`, `journal`, `borrado_recursivo`, `distributed_locks`, `lock_cancelacion_adquisicion`) | ver §4 (corrida en el árbol mergeado) |
| Baseline del subsistema sobre la base (grass + BDD) | **409 passed, 21 skipped** |

## 4. Mutación dirigida (7/7 muertos)

Arnés textual (aplicar → correr → restaurar, escritura con `write_bytes` para no traducir fin de
línea). Baseline del subconjunto: **329 passed**.

| Mutante | Mutación | Resultado |
|---|---|---|
| M1 | veredicto sin regla de procedencia (`new` acepta cualquier cosa) | **MUERTO** — 2 tests |
| M2 | `.fail` ignorados | **MUERTO** — 3 tests |
| M3 | sin corte por `stall_progress_s` | **MUERTO** — 1 test |
| M4 | `new` acepta archivos heredados | **MUERTO** — 1 test |
| M5 | el servicio fuerza `certificado=True` | **MUERTO** — 4 tests |
| M6 | no se verifica terminalidad | **MUERTO** — 3 tests |
| M7 | el veredicto se cierra con `limpieza_limpia=True` sin mirar la neutralización | **MUERTO** — 1 test |

## 5. Vigilancia de progreso (F-05) — comportamiento

- Muestreo cada `progress_poll_interval_s` (**30 s**), en hilo: nombres `.cgid`/`.dgid`/`.fail` (sin
  `stat` por archivo) + `stat` del flag + lectura **incremental** de la cola del flag (tope 1 MiB) para
  recolectar líneas `ws_`.
- Corte sólo si: proceso vivo **y** pasó `stall_grace_s` (**1200 s**) desde el spawn **y** no hubo
  **ningún** cambio en `stall_progress_s` (**600 s**). NGIO usa 60 s para su propio freeze-check: el
  umbral es 10× más conservador, así que el watchdog nunca compite con él.
- Reacción: **una** pasada de `_matar_todo`, verificación de terminalidad, `outcome="stalled"`, **sin
  relanzar** y con el caché parcial conservado. El deadline global no se extiende.
- El poll usa `_sleep_or_cancel`: la cancelación despierta al instante y el event loop queda libre para
  renovar los leases `grass-cache`/`load-order`.
- Terminalidad: `terminado` (PID ausente **o** PID reciclado por otro proceso), `vivo` (es el juego
  atribuido tras la espera) o `indeterminado` (no se pudo leer nombre/exe/`create_time`). `vivo`/
  `indeterminado` ⇒ `verdict="unknown"` y **no** se relanza ni se certifica.

## 6. Gates ejecutados

| Gate | Comando | Resultado |
|---|---|---|
| Ruff check | `ruff check sky_claw/ scripts/ tests/` | **All checks passed!** |
| Ruff format | `ruff format --check sky_claw/ scripts/ tests/` | **1197 files already formatted** |
| Mypy | `mypy sky_claw/ scripts/` | **Success: no issues found in 479 source files** (el write-set es estricto) |
| Focalizados | `pytest` de §3.2 + anclas | ver §3.2/§4 (árbol mergeado) |
| Suite completa | `pytest -q` | ver «Resultado de la suite» al pie |
| Integración | `git merge origin/main --no-ff` | dos padres; `pyproject.toml` auto-mergeado revisado a mano (las dos entradas conviven) |

**Comparación de fallos:** la suite completa se corre sobre el árbol mergeado; si aparece algún fallo,
se compara por **node ID** contra la misma corrida sobre `origin/main` (no por número).

## 7. Límites declarados (no son verdes)

1. **Nada se midió en Windows con MO2/USVFS/SKSE/NGIO reales.** Los tests son unitarios con mundos de
   procesos simulados y fixtures binarios; el veredicto no está certificado en rig.
2. **No hay oráculo de completitud de celdas.** `completitud_de_celdas` es siempre `no_verificada` y
   `completed_verified` significa «sin huecos conocidos», no «cada celda generada».
3. **Cobertura USVFS**: sigue `no_verificada` (P2). El veredicto no la toca.
4. **Los worldspaces declarados por NGIO** (líneas `ws_` del flag) son un **límite inferior**: el
   archivo bufferiza y desaparece en `End()`. Se reportan como evidencia y **no** como requisito.
5. **NGIO 2.x no está inspeccionado** en su comportamiento (el contrato se ancla en `1.0.13`, la
   versión que el instalador descarga). El original de Nexus (2.2.0/2.3.0) no tiene fuente accesible.
6. **El barrido de flag huérfano** (`reconcile_orphan_precache_flag`) sigue borrando sin archivar
   progreso: es una causa de desaparición del flag distinta de `End()` y quedó **fuera** de P1 a
   propósito (decisión de alcance; requiere tocar el arranque).
7. **`etapas_previas_lods`** sigue probando «existe algún `.cgid`» y **no** consume el veredicto:
   convertir esa precondición en consumidor exige un registro de corridas por etapa que no existe.
8. **`MaxFailures` efectivo** en un perfil con otros mods que aporten `GrassControl.ini` no es
   verificable desde acá; por eso cualquier `.fail` presente bloquea la certificación, sin suponer el
   umbral.

## 8. Registro de integración (R1–R15)

| Regla | Hecho |
|---|---|
| R1 — merge, no rebase | `git merge origin/main --no-ff` sobre la rama; historia intacta |
| R2 — `--no-ff` | el commit de sincronización tiene **dos padres** (`429b0e8c`, `a378b19b`) |
| R3 — el PR no se mergea acá | no se tocó `main`; sin squash, sin force-push |
| R4 — `origin/main` ancestro | verificado tras el merge (`merge-base --is-ancestor`) |
| R5/R6 — clasificación | **sin conflictos**; el único archivo compartido con main es `pyproject.toml` (ambos agregan una entrada a la lista estricta), revisado a mano: conviven |
| R7 — un merge limpio no es evidencia | inventario de call sites rehecho sobre el árbol mergeado: `GrassCacheRunResult` sólo lo construye el runner (y el doble del BDD, actualizado); `evaluar_veredicto` sólo lo llama el runner; `GRASS_VERDICT_KIND` sólo el servicio; `begin_response`/journal no cambiaron de firma |
| R10 — call sites | `run()`/`_salida_exito`/`_vigilar` son privados; `_resultado` sólo se usa dentro del runner; la strategy no se tocó |
| R13 — registro | este documento + cuerpo del PR |
| R15 — anclas | todos los gates corren sobre el árbol **mergeado** |

## 9. Resultado de la suite

- **CI del PR #1027 (head `8e2341b1`): 24 checks verdes, 0 rojos.** Incluye `Shard 1/6 … 6/6 / py3.11`
  (Windows), `Tests (Pytest)`, `Tests / windows-latest`, `POSIX Core`, `Lint (Ruff)` ×2,
  `Type Check (Mypy)` ×3, `Security Scan`, CodeQL ×4, `Regression & Test Oracle`, `Auto-revisión en
  PR interno` y `Main en verde`.
- **Head `5261ecf8` (con las sincronizaciones):** shard 1/6 rojo por un test **ajeno**,
  `tests/test_dyndolod_progreso.py::test_un_callback_que_cuelga_una_vez_no_apaga_los_latidos_siguientes`
  (`assert 1 >= 3`, test de latidos con tiempos reales). Pasa 3/3 en local, el archivo no menciona
  grass: es un **flake preexistente por carga**, no una regresión de este PR. Se re-corre el job.
- **Corrida local sobre el árbol integrado** (basetemp aislado por corrida — dos `pytest` simultáneos
  comparten `.pytest-tmp` y se pisan; quedó documentado como riesgo del arnés):
  - núcleo P1 (`verdict` + `runner` + `service`): **349 passed**;
  - resto de focalizados (`safety`, `profile`, `bdd`, `journal`, anclas AST): **310 passed, 25 skipped**;
  - `ruff check` / `ruff format --check` (1207 archivos): verde;
  - `mypy sky_claw/ scripts/`: **483 archivos, 0 errores** (el write-set es estricto, sin exención).
- **La suite completa local de un solo proceso** no se completó (~65 min en Windows); la cobertura
  equivalente la dan los 6 shards de CI. Por eso no hay número local de suite completa: el número de
  CI es el que vale.

### 9.1 Qué lleva cada superficie (contrato real, no «todo en todas»)

El PR decía «la evidencia viaja al resultado, evento y journal»; es impreciso. El contrato es:

| Superficie | Qué lleva |
|---|---|
| Resultado del servicio | **todo**: `verdict`, `certificado`, `verdict_razones`, `alcance_certificado`, `evidencia` completa (identidad, inventarios, `.fail` atribuibles/históricos/no evaluables, worldspaces, progreso, terminalidad) |
| Evento `pipeline.grass_cache.completed` | `success`, `outcome`, `duration_seconds`, `verdict`, `certificado`, `alcance_certificado` — **no** la evidencia completa: un suscriptor no puede inferir certificación de `success` (D1) |
| Operación del journal (`GRASS_VERDICT_KIND`) | `kind`, `verdict`, `certificado`, `razones`, `alcance_certificado`, `mode`, `worldspaces` — **no** la evidencia completa ni `files_touched` (sembrar artefactos falsearía el oráculo de recovery) |

## 10. Qué NO se hizo (fuera de alcance, declarado)

- P2 (atestación de load order / USVFS), P3 (instalador/INI), P4 (publicación en mod), P5 (GUI/agente).
- Corrección del autoinstalador: sus defectos quedaron registrados para P3 (informe aparte).
- `reconcile_orphan_precache_flag` archivando progreso antes de borrar (punto 6 de §7).

## 11. Ronda de revisión independiente (H1–H7) — correcciones

Dictamen recibido: **BLOCKED / REQUEST CHANGES** sobre `8e2341b1`. La arquitectura de P1 quedó
aceptada; se corrigieron los siete hallazgos, cada uno **reproducido antes** de tocar código.

### 11.1 Evidencia RED → GREEN por hallazgo

| # | Defecto reproducido (rojo) | Corrección | Test que lo fija |
|---|---|---|---|
| **H1** | Un `.fail` cuyo `read_bytes()` falla **desaparecía** del diccionario de fallos aunque `fail_count=1`: la política devolvía `completed_verified` / `sin_fallos_conocidos` | El inventario cuenta `fallos_no_evaluables` en vez de tragárselos, y la política tiene **defensa en profundidad**: `no_evaluables = max(ev.fallos_no_evaluables, fail_count - len(fallos_finales))`. Un `.fail` sobreviviente no evaluable **prohíbe certificar** (`fallos_de_celda_no_evaluables`) sin inventar su contenido | `test_h1_inventario_cuenta_los_fail_no_evaluables` (unitario del inventario), `test_h1_fail_ilegible_no_certifica`, `test_h1_fail_contado_pero_no_inventariado_no_certifica`, `test_h1_fail_no_evaluable_no_atribuye_de_mas` |
| **H2/H5** | Toda la familia de salidas construía el resultado **sin verificar terminalidad**; `_verificar_terminalidad(None)` devolvía `terminado` aunque hubiera un proceso atribuido antes | **Único cierre terminal** `_cerrar_y_verificar()` (kill + terminalidad) usado por cancelación cooperativa, disco lleno, deadline, stall, spawn fallido, agotamiento de reintentos y cortes de contrato; `_ultimo_atribuido` permite verificar aunque el caller pierda el handle. El camino de **excepción/cancelación dura** cierra **síncrono** (un `await` ahí enmascararía la excepción original): kill directo + una clasificación, `indeterminado` si no se puede afirmar | `test_h2_cancelacion_con_juego_no_verificable_es_unknown`, `test_h2_disco_lleno_con_juego_no_verificable_es_unknown`, `test_h2_corte_por_progreso_perdido_con_juego_no_verificable_es_unknown`, `test_h2_spawn_fallido_no_es_unknown`, `test_p1_terminalidad_*` |
| **H3** | La gracia del watchdog llamaba a `_muestrear_progreso(linea_base=True)`: **descartaba** el progreso de los primeros 1 200 s. Un RESUME que sólo avanza en esa ventana quedaba `sin_evidencia_de_esta_corrida` | La gracia suspende **sólo la decisión**: la muestra se toma y compara siempre; el corte se evalúa después de la gracia | `test_h3_progreso_durante_la_gracia_se_registra` |
| **H4** | `_capturar_baseline_sync` degradaba **cualquier** `OSError` a baseline vacío: un archivo preexistente pasaba a «nuevo» y producía `completed_verified` | Distingue **caché inexistente** (`FileNotFoundError`/`NotADirectoryError` ⇒ vacío legítimo) de **ilegible** (cualquier otro `OSError` **propaga**). Si el baseline no se puede verificar, el run **aborta antes de escribir el flag** con `baseline_ilegible` ⇒ `unknown` | `test_h4_baseline_ilegible_aborta_antes_del_spawn`, `test_h4_capturar_baseline_no_silencia_un_error_de_listado`, `test_h4_baseline_ilegible_no_certifica` |
| **H6** | El servicio copiaba el veredicto del runner sin reconciliarlo: con lease perdido o flag residual, el dict podía seguir diciendo `certificado=True` y el journal registrar la certificación en una TX revertida | El resultado se compone **antes** del cierre; si `exito=False` y el runner certificó, `invalidar_certificacion(razon)` degrada a `completed_unverified` con `operacion_invalidada` / `limpieza_incompleta`, y **journal, evento y dict** usan el veredicto reconciliado. La evidencia observada **no se falsea** (los ejes de trabajo/integridad se preservan) | `test_h6_perdida_de_lease_invalida_la_certificacion`, `test_h6_limpieza_incompleta_invalida_la_certificacion`, `test_h6_certificacion_valida_se_mantiene`, `test_h6_invalidar_certificacion_degrada_y_agrega_razon` |
| **H7** | El test del watchdog aceptaba `outcome != "stalled" or progreso_observado is True`: un stall **prematuro** después de ver algo de progreso pasaba | Tests deterministas con **reloj controlado** (`_RelojFalso` sobre el `time` del módulo) y secuencia de muestras: no corta antes del umbral desde el último avance; un avance **reinicia** el presupuesto; corta después del umbral sin avance | `test_h7_no_corta_antes_del_umbral_desde_el_ultimo_avance`, `test_h7_avance_reinicia_el_presupuesto`, `test_h7_corta_despues_del_umbral_sin_avance` |

### 11.2 Matriz de terminalidad (estados y efecto)

| Situación | Terminalidad | Veredicto |
|---|---|---|
| No se intentó lanzar, o la ventana de spawn se agotó **sin proceso atribuido** (lanzamiento no efectuado) | `terminado` | sigue el `outcome` (`failed` para `spawn_failed`) |
| Proceso atribuido y **muerto** (o PID reciclado por otro proceso) | `terminado` | normal |
| Proceso vivo tras la espera acotada (`terminality_wait_s`) | `vivo` | `unknown` |
| No se puede leer nombre/exe/`create_time` del PID | `indeterminado` | `unknown` |
| Excepción / cancelación dura | clasificación **síncrona**; `indeterminado` si no se puede afirmar | se propaga la excepción (no se publica resultado) + warning |

### 11.3 Mutantes ejecutados (7/7 muertos)

Baseline del subconjunto (`verdict` + `runner` + `service`): **349 passed**.

| Mutante | Resultado |
|---|---|
| M_H1 · suprimir la lectura de `.fail` (volver a `suppress`) | **MUERTO** (1 test) |
| M_H1b · anular la defensa de conteo de la política | **MUERTO** (2 tests) |
| M_H4 · baseline ilegible degradado a vacío | **MUERTO** (1) |
| M_H2 · terminalidad asumida por el pedido de kill | **MUERTO** (4) |
| M_H3 · gracia que descarta el progreso | **MUERTO** (1) |
| M_H6 · certificación sin reconciliar en el servicio | **MUERTO** (2) |
| M_H7 · stall prematuro (mitad del umbral) | **MUERTO** (2) |

> El mutante M_H1 **sobrevivía** en la primera corrida: la defensa de conteo de la política enmascaraba
> la supresión en el runner. Se agregó el test **unitario del inventario** que discrimina la evidencia
> (`.fail` contado, contenido no leído, `no_evaluables=1`), y entonces muere.

### 11.4 Incidentes declarados de la ronda

1. **Corrupción de codificación auto-detectada.** Una edición mía con heredoc de PowerShell dejó
   `grass_cache_verdict.py` **doble-codificado** (utf-8 → cp1252 → utf-8, 96 marcas) y ese estado se
   commiteó en `bfb94cfa`. Se detectó al revisar el archivo, se reparó invirtiendo la transformación y
   se verificó que **el resto del write-set está limpio** (0 marcas). No se reescribieron commits
   publicados: la reparación es un commit nuevo.
2. **Dos `pytest` concurrentes comparten `.pytest-tmp`** y se pisan (173 errores de teardown en una
   corrida anterior). Regla operativa: `--basetemp` único por corrida; quedó anotado acá porque no es
   detectable desde el código.
3. **`terminality_wait_s` en tests.** El default de producción (10 s) no cabe en tests con
   `max_runtime_s` de décimas: el fixture lo baja a 0,2 s. Los casos que necesitan esperar lo suben
   explícito.
4. **`main` rojo ajeno a P1.** El gate `Main en verde` falló por `tests/test_bodyslide_journal.py`
   (`database is locked`, run 45677510174, material de #1024). Se documentó en el PR; al repararse
   `main`, el gate pasó.

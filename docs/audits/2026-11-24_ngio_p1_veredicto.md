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

_(se completa con la corrida sobre el árbol mergeado; si hay fallos se comparan por node ID)_

## 10. Qué NO se hizo (fuera de alcance, declarado)

- P2 (atestación de load order / USVFS), P3 (instalador/INI), P4 (publicación en mod), P5 (GUI/agente).
- Corrección del autoinstalador: sus defectos quedaron registrados para P3 (informe aparte).
- `reconcile_orphan_precache_flag` archivando progreso antes de borrar (punto 6 de §7).

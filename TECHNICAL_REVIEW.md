# Technical Review — Sky-Claw: Ingeniería de Software × Modding de Skyrim SE/AE

**Fecha:** 2026-07-06
**Rama:** `claude/skyclaw-technical-review-k6lqub` (sobre `main @ 5bf5870`)
**Alcance:** todo el repo (`sky_claw/`, `tests/`, `pyproject.toml`, CI), contrastado con dos análisis externos previos ("codex", "codex2") y el informe técnico de modding adjunto (Bashed vs Smashed, `Manual Cost Calc`, symlinks/VFS, CAO).
**Método:** verificación estática de cada afirmación contra el código real (grep + lectura dirigida). No se ejecutó ninguna herramienta sobre una instalación real de Skyrim/MO2; no se corrió la suite de tests en este contenedor. Cada hallazgo cita `archivo:línea` reproducible.

---

## 1. Resumen Ejecutivo

**Veredicto: viable y con base de ingeniería seria, pero en estado de release candidate para usuarios avanzados — no GA.** Coincide con la conclusión de ambos análisis de Codex, y esta revisión la confirma contra el código: la capa de *orquestación segura* (locks, snapshots, HITL, sandbox de paths, egress control, DLQ, ~2.400 tests, CI con Ruff/Mypy/Pytest/Bandit/pip-audit) es real y está bien construida. Lo que falta es la capa de *criterio experto de modding*: conocimiento semántico por subrecord, preflight de infraestructura (VFS/symlinks), y validación de flags críticos como `Manual Cost Calc`.

**Hallazgo nuevo de esta revisión, no reportado por Codex y de severidad P0:** el script `apply_leveled_list_merge.pas` — cableado a la estrategia `CreateMergedPatch` del `PatchOrchestrator` — no fusiona leveled lists: copia al plugin de salida la **primera** versión de cada FormID que encuentra en el orden de iteración (típicamente el master base, es decir, el *perdedor* según reglas de load order) y descarta todos los overrides posteriores como "duplicados". Como el parche de salida carga último, el resultado puede **revertir** los cambios de leveled lists de toda la modlist — peor que no parchear. Detalle en §4.1.

En síntesis: Sky-Claw hoy es una excelente **plataforma de orquestación** y un **arquitecto de load order incompleto**. El camino a "must-have" no es agregar más herramientas, sino hacer que las que ya orquesta produzcan resultados *correctos y explicables*.

### Contraste con los análisis previos

| Fuente | Aciertos verificados | Donde se queda corto |
|---|---|---|
| codex.txt | Brechas de preflight VFS, reglas semánticas, monolitos, 199 `except Exception` | No detectó que el merge de leveled lists es funcionalmente incorrecto (lo trata como brecha de features, no como bug) |
| codex2.txt | LOOT sin snapshot, SCPT/SCEN, "el .pas parece copiar records, no fusionar", mypy `ignore_errors` (~1.684 errores) | Subestimó el bug del .pas: no es solo "copiar en vez de fusionar" — la lógica de duplicados invierte el ganador |
| mmodding.txt | Marco correcto: Manual Cost Calc, LOOT ≥0.29 vs symlinks, Bashed vs Smashed, CAO/header 44 | Es doctrina, no diagnóstico: nada de eso está implementado aún en Sky-Claw (verificado: 0 hits de `Manual Cost Calc`, CAO ni preflight en el código) |

---

## 2. Análisis de Brechas

Cada fila está anclada a evidencia en el código. "Prioridad" = urgencia para llegar a GA.

| # | Área | Lo que tiene (verificado) | Lo que falta / riesgo (verificado) | Prioridad |
|---|---|---|---|---|
| 1 | **Parcheo de leveled lists** | Estrategia `CreateMergedPatch` (prioridad 10, fallback) en `sky_claw/local/xedit/patch_orchestrator.py:227` | **Bug P0:** `scripts/apply_leveled_list_merge.pas:166-172` salta overrides por FormID duplicado → gana la primera versión iterada, no la ganadora; nunca llama a `WinningOverride`; sin semántica Relev/Delev. Puede revertir la modlist | **P0 — bloqueante** |
| 2 | **Rollback de LOOT** | `SnapshotTransactionLock` serializa la ejecución (`sky_claw/local/tools/loot_service.py:142-147`) | `target_files=[]` — snapshot de `plugins.txt`/`loadorder.txt` diferido a propósito (documentado en el docstring del módulo). Si LOOT corrompe el orden, no hay rollback | Alta |
| 3 | **Semántica de records** | Clasificación de severidad por *tipo* de record (`conflict_analyzer.py:374`, `_classify`) | Sin análisis ganador/perdedor por subrecord; sin detección de `Manual Cost Calc` (0 hits en código y tests); sin tags tipo Bash (Relev/Delev/Stats) | Alta |
| 4 | **Consistencia de firmas** | `conflict_analyzer.py:34` usa `SCEN` (comentario SCA-001: SCPT obsoleto) | `patch_orchestrator.py:342,471` y `scripts/list_all_conflicts.pas:24` siguen usando `SCPT`, record inexistente en Skyrim SE — la rama "alto riesgo" por SCPT es código muerto | Media |
| 5 | **Preflight de infraestructura** | Symlink handling existe pero como *sandbox de seguridad* (`security/path_validator.py:133-162`) | No existe el concepto "preflight" en el código (0 hits). Sin health-check de symlinks/junctions en rutas de juego/MO2 ni chequeo de versión de LOOT (≥0.29 por el bug de libloot con symlinks) | Alta |
| 6 | **Cobertura de herramientas** | LOOT, xEdit, Wrye Bash, Synthesis, DynDOLOD, BodySlide, Pandora, VRAMr (`sky_claw/local/tools/`) | Sin Mator Smash (solo una string de recomendación en `conflict_analyzer.py:351`), sin CAO, sin inspección de header 43/44, sin ESL-flag real por header (solo por extensión `.esl`, `conflict_analyzer.py:207`) | Alta |
| 7 | **Robustez de errores** | Contrato `success`+`message` normalizado (`tool_result.py`, resuelto #5 de CLAUDE.md) | 199 `except Exception` en `sky_claw/`; BLE001 desactivado con 177 violaciones documentadas (`pyproject.toml:110`) — riesgo de tragar fallos de herramientas externas | Alta |
| 8 | **Tipado** | mypy en CI; módulos nuevos estrictos (`pyproject.toml:208-216`) | `ignore_errors = true` global con ~1.684 errores en ~30 módulos pendientes (`pyproject.toml:192`) | Media |
| 9 | **Arquitectura** | Tool registry, event bus, DLQ, journal, locks, snapshots, router multi-LLM | Monolitos: `forge_dashboard.py` (1.536 líneas), `state_graph.py` (1.360), `xedit/runner.py` (1.093), `journal.py` (1.055), `sky_claw_gui.py` (1.046), `app_context.py` (844) | Media |
| 10 | **GUI / accesibilidad** | Tema consistente, paneles de rituales, aprobaciones HITL, chat | 7 usos de `transition: all`, 0 `prefers-reduced-motion`, sin virtualización de listas largas | Media |
| 11 | **Validación end-to-end** | ~2.389 funciones de test; CI verde (Ruff/Mypy/Pytest/Bandit/pip-audit) | Los tests mockean los subprocesos: prueban argumentos, no efectos reales. Sin smoke documentado en rig Skyrim+MO2 real (el propio CLAUDE.md lo lista como pendiente para QuickAutoClean) | **Bloqueante antes de GA** |
| 12 | **Dependencias de scripts** | Scripts Pascal generados y estáticos en `local/xedit/scripts/` | `uses mteFunctions` (`apply_leveled_list_merge.pas:22`, `runner.py:187,247,326`) — mteFunctions.pas no viene con xEdit; sin chequeo de que exista, los scripts fallan al compilar en instalaciones limpias | Media |

---

## 3. Evaluación por perspectiva

### 3.1 Ingeniería de Software Senior

**Lo bueno (y es mucho):** la arquitectura async con boundaries claros entre GUI/orquestador/runners, el patrón `SnapshotTransactionLock` para operaciones destructivas, el contrato normalizado de resultados de tools (deuda #5 cerrada de raíz, no parcheada), el egress control con allowlist y anti-DNS-rebinding (commit `5bf5870`), y una disciplina de PRs pequeños con historia legible. La densidad de tests (~2.389) y el CI multi-gate están por encima de la media de proyectos de modding.

**Lo preocupante:** la calidad es *desigual por capa*. El núcleo de seguridad y transacciones está pulido; la capa de conocimiento de dominio (scripts Pascal, clasificación de conflictos) tiene bugs funcionales y código muerto (SCPT). Los 199 `except Exception` son especialmente peligrosos en los runners: una herramienta externa que falla a mitad de una mutación de load order no debe ser tragada y resumida. Y el patrón de test "mockear el subproceso" da una falsa señal de cobertura sobre exactamente la parte que más importa: el efecto real sobre los archivos del juego.

### 3.2 Usuario veterano / Modder de Skyrim

**Lo bueno:** MO2-first es la decisión correcta; HITL antes de descargas externas es exactamente lo que un modder paranoico quiere; la búsqueda en lenguaje natural sobre Nexus baja la barrera de entrada; y serializar BodySlide/herramientas bajo lock evita el clásico "corrí dos tools a la vez y me destrocé el overwrite".

**Lo que rompe la confianza:** un veterano abre el "merged patch" de Sky-Claw en xEdit, ve que las leveled lists del parche son las del master base, y desinstala la herramienta para siempre — la confianza en modding no se recupera. Además, hoy Sky-Claw no responde las preguntas que un modder hace antes de tocar nada: ¿mi ruta de juego tiene symlinks que ciegan a LOOT? ¿mi LOOT es ≥0.29? ¿este parche preserva el `Manual Cost Calc` de Sustained Magic o me va a dejar hechizos de 30.000 de Magicka? ¿cuándo conviene Bashed vs Smashed vs Synthesis? La herramienta ejecuta, pero no *explica*, y en Skyrim confianza = trazabilidad.

---

## 4. Plan de Acción Técnico (Ingeniería)

En orden de prioridad estricto:

### 4.1 — P0: Arreglar o retirar `apply_leveled_list_merge.pas`

El bug, paso a paso (`sky_claw/local/xedit/scripts/apply_leveled_list_merge.pas`):

1. `Process(e)` recibe cada record en orden de iteración de xEdit (masters primero).
2. La primera versión de un FormID (la del master base = **perdedora** por load order) se copia al plugin de salida vía `wbCopyElementToRecord` (línea 183).
3. Toda versión posterior del mismo FormID — los overrides de los mods, los **ganadores** — se descarta en `RecordExistsInOutput` (líneas 166-172) como "Skipped duplicate".
4. El plugin de salida carga último → sus records (versión base) ganan → la modlist pierde sus cambios de leveled lists.
5. `CleanMasters` al final (línea 261) sobre ese contenido agrava el riesgo de masters mal declarados.

**Acción mínima (hotfix):** procesar solo `WinningOverride(e)` y saltar el resto — eso convierte el script en un "forward del ganador", que es inocuo aunque siga sin fusionar.
**Acción correcta:** implementar merge real de entradas LVLI/LVLN/LVSP (unión de entradas de todos los overrides, con semántica Relev/Delev), o retirar la estrategia y delegar leveled lists explícitamente al Bashed Patch de Wrye Bash, que ya está integrado (`wrye_bash_runner.py`). Mientras tanto, **deshabilitar la estrategia `CreateMergedPatch`** (`patch_orchestrator.py:227`).
**Test que falta:** un test rojo que cargue dos overrides del mismo LVLI y verifique que el output contiene las entradas fusionadas (o al menos el ganador), no la versión base.

### 4.2 — Snapshot real de load order para LOOT

`loot_service.py:147` difiere el snapshot (`target_files=[]`) porque la ruta del profile no siempre se conoce. Resolverlo: derivar `plugins.txt`/`loadorder.txt` del profile activo de MO2 (el repo ya parsea `modlist.txt` con BOM preservado — reutilizar esa infraestructura de `local/mo2/`) y pasarlos como `target_files`. Con eso, LOOT queda al mismo nivel de protección que xEdit/Synthesis/QuickAutoClean, que ya tienen rollback.

### 4.3 — Unificar SCPT → SCEN

Aplicar la decisión SCA-001 (`conflict_analyzer.py:34`) en `patch_orchestrator.py:342,471` y `scripts/list_all_conflicts.pas:24`. Hoy la rama de "alto riesgo por SCPT" del orquestador es inalcanzable en Skyrim SE y la del Pascal clasifica mal.

### 4.4 — Reducción dirigida de `except Exception`

No hace falta un big-bang: activar `BLE001` por carpeta empezando por `sky_claw/local/tools/` y `sky_claw/local/xedit/` (los runners de herramientas externas, donde tragar errores es más caro), convirtiendo cada caso en excepciones tipadas del contrato `success`+`message` ya existente. Mismo enfoque incremental para `ignore_errors` de mypy, módulo por módulo, como ya documenta el TODO de `pyproject.toml:192`.

### 4.5 — Descomponer monolitos con boundaries de dominio

Prioridad: los que tocan procesos externos y transacciones (`xedit/runner.py`, `journal.py`, `state_graph.py`) antes que la GUI. Extraer Protocols pequeños (`ToolRunner`, `LoadOrderSnapshotService`, `PluginHeaderInspector`, `VfsHealthChecker`) — los dos últimos además habilitan los items §5.1 y §5.2.

### 4.6 — Pipeline auditable por fases

Formalizar `preflight → scan → plan → approve → execute → validate → rollback/commit`, cada fase emitiendo un manifiesto JSON (archivos, plugins, herramienta+versión, cambios previstos). El repo ya tiene las piezas (journal, snapshots, preview chain de `orchestrator/preview/`); falta el contrato de fases explícito y el manifiesto persistido.

### 4.7 — Criterio de GA: matriz de validación real

Perfiles MO2 descartables: (a) vanilla+USSEP, (b) lista media con overhauls de magia/perks (Mysticism/Vokrii/Sustained Magic — el caso `Manual Cost Calc`), (c) lista grande con DynDOLOD. Criterio de aprobación: run completo sin mutaciones no aprobadas + rollback probado + diff de `plugins.txt`/`modlist.txt`/`overwrite` explicable. El smoke de QuickAutoClean que CLAUDE.md ya lista como pendiente entra en esta matriz.

---

## 5. Plan de Acción UX y Compatibilidad (Modding)

### 5.1 — Preflight de Modlist (la feature que falta antes que ninguna otra)

Chequeo previo a cualquier Ritual: symlinks/junctions/reparse points en rutas de juego, MO2, `mods/`, `profiles/`, `overwrite/` (el informe de modding documenta que libloot <0.29 se sale del VFS al resolverlos); versión de LOOT instalada; límites de plugins full/light; masters faltantes; herramientas presentes (incluido `mteFunctions.pas`, §2.12). Resultado: semáforo verde/amarillo/rojo *antes* de tocar nada. Nota: el manejo actual de symlinks (`path_validator.py`) protege a Sky-Claw de escaparse del sandbox — esto es lo inverso, proteger al *usuario* de una infraestructura que ciega a las herramientas.

### 5.2 — Inspección real de headers de plugin

Leer el header TES4: ESL-flag real (no solo extensión `.esl` como hoy en `conflict_analyzer.py:207`), versión 43 (LE) vs 44 (SSE), FormIDs fuera de rango para compactación segura. Habilita las recomendaciones "convertir a ESPFE" y "este plugin es de Oldrim, pasalo por CK/CAO" con evidencia en vez de heurística.

### 5.3 — Detección de flags críticos, empezando por `Manual Cost Calc`

Regla declarativa sobre records SPEL combinados: si algún override define coste manual y el ganador del merge no preserva el flag, alertar con explicación ("Sustained Magic define coste manual; sin este flag el motor recalcula por duración infinita → coste astronómico"). Es el ejemplo canónico del informe adjunto y hoy hay 0 soporte. Generalizar después a LVLI/LVLN/LVSP (Relev/Delev), PERK, MGEF.

### 5.4 — Asistente de estrategia de parcheo

Reemplazar el botón genérico "Crear Parche" por recomendación explícita con justificación: Bashed Patch para leveled lists/tweaks; Smashed Patch (cuando se integre Mator Smash) para records complejos multi-overhaul; Synthesis para patchers conocidos; xEdit manual para conflictos críticos. Hasta que el merge propio sea correcto (§4.1), Sky-Claw no debería ofrecer su propio merged patch como opción por defecto.

### 5.5 — Panel de conflictos por subrecord ("xEdit-lite")

Evolucionar el reporte actual (pares de plugins + severidad por tipo de record) a: record → subrecord → ganador → perdedores → por qué → qué se preserva/pierde → parche sugerido. Es la diferencia entre "hay 12 conflictos críticos" y una decisión informada.

### 5.6 — Rule packs de compatibilidad curados

Empezar por los que más soporte generan: USSEP, SKSE/Address Library, SkyUI/MCM Helper, SPID/KID, Nemesis/Pandora/OAR, DynDOLOD, Lux/ELFX, JK's Skyrim, Requiem, SimonRim/EnaiRim, Mysticism/Vokrii/Ordinator/Apocalypse/Sustained Magic. Formato declarativo (TOML/JSON) para que la comunidad contribuya sin tocar Python.

### 5.7 — Post-run validator

Tras cada Ritual: límites full/light, masters faltantes, plugins header 43, parches sin ESL-flag que podrían llevarlo, overwrite sucio, logs de LOOT/xEdit con warnings, conflictos críticos restantes. Cierra el loop `validate` del pipeline §4.6 y es lo que convierte "terminé" en "terminé y está sano".

### 5.8 — Accesibilidad y rendimiento de GUI

Reemplazar los 7 `transition: all` por transiciones de propiedades específicas; agregar `prefers-reduced-motion`; virtualizar listas de mods >50 items; labels reales en vez de placeholders; focus visible. Costo bajo, señal de calidad alta.

---

## 6. Qué NO recomiendo hacer ahora

- **No** perseguir "autonomía total" del agente: el modo seguro (preview + aprobación) es el producto correcto para este dominio; la decisión lock-only sin HITL en la capa LLM ya está documentada y es sensata.
- **No** integrar Mator Smash antes de arreglar §4.1: agregar un merger más potente sobre una base de merge incorrecta multiplica el daño.
- **No** refactorizar la GUI monolítica antes que los runners: el riesgo vive donde se mutan archivos del juego.

## 7. Supuestos y límites de esta revisión

- Revisión estática sobre el repo en `main @ 5bf5870`; no se ejecutaron herramientas contra una instalación real de Skyrim/MO2 ni la suite de tests en este entorno (sin venv en el contenedor; los ~2.389 tests se contaron por `def test_` y coinciden en orden de magnitud con los 2.435-2.451 colectados reportados por los análisis previos con CI verde).
- El comportamiento descrito del script Pascal (§4.1) se deriva de la semántica documentada de xEdit scripting (orden de iteración, `wbCopyElementToRecord`, ausencia de `WinningOverride`); el smoke real en xEdit confirmaría el efecto exacto sobre el plugin de salida.
- Los análisis de Codex citados se tomaron como hipótesis a verificar, no como fuente de verdad; todo lo afirmado aquí tiene referencia propia a `archivo:línea`.

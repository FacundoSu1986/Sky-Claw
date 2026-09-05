# OODA — inventario vivo de pendientes

> **Audiencia:** maintainers, reviewers y agentes.
>
> **Estado:** Parcial.
>
> **Fuentes canónicas:** código y tests del árbol actual; ADRs aceptados para
> decisiones; este documento únicamente consolida su estado.
>
> **Última verificación:** 2026-08-08 en la rama actual, basada en
> `origin/main` `53083c0`; los cambios posteriores de esta rama no se afirman
> integrados en `origin/main`.
>
> **Re-baseline parcial 2026-08-16 sobre `main` `f8e8a4f`:** cubre las filas de la
> etapa 9 de DynDOLOD y nada más —
> «Clasificación del log de la etapa 9», «Candidato de salida de TexGen», la
> corrección de `U-06`, y la fila «Preset de TexGen desvía `OutputPath`» **con su
> sección propia**, donde se corrigió el alcance de la contención del gate de
> frescura. **Todas** las referencias a línea de esas cuatro filas y de esa sección
> —incluidas las de `run_full_pipeline` y los dos call sites de
> `_package_output_as_mod`— se releyeron contra `f8e8a4f`; ninguna arrastra el
> baseline de 2026-08-08. **No** es una reverificación integral del resto de la
> tabla.
>
> **Re-baseline parcial 2026-08-30 sobre `main` `89d81ad2` (#522):** cubre exclusivamente
> las filas `T-22` y `T-24` — `prefers-reduced-motion` sobre animaciones decorativas y
> foco visible de teclado (incluyendo inputs Quasar), ancladas en
> `tests/test_gui_theme_contracts.py`. Ambas pasan a **Parcial**; no afirma el cierre
> integral de transiciones ni el inventario exhaustivo de formularios/labels. **No** es una
> reverificación integral del resto de la tabla.
>
> **Re-baseline parcial 2026-09-04 sobre `main` `c4df8ce`:** cubre exclusivamente la
> fila `F9` (Strangler Fig del composition root de `SupervisorAgent`), revalidada contra
> los PR mergeados #518, #520, #524, #526/#527, #540 y **#545** —este último ya mergeado
> por squash en `c4df8ce`, con `RecordConflictScanner` integrado en `main`
> (`sky_claw/app/orchestrator/record_conflict_scan.py`)— y contra el árbol actual de
> `sky_claw/app/orchestrator/`. Pasa de **Abierto** a **Parcial**. **No** es una
> reverificación integral del resto de la tabla.

La narrativa fechada, las refutaciones y la secuencia completa de decisiones se
preservan en el [historial OODA de julio de
2026](audits/2026-07_historial_ooda.md). Antes de iniciar un ítem, volver a
confirmarlo contra código y tests.

## Estado consolidado

<!-- markdownlint-disable MD013 -->

| Ítem | Estado | Cerrado en | Qué falta | Verificado por |
|---|---|---|---|---|
| T-10 | Parcial | `local/tools/` | Retirar BLE001 incrementalmente del resto del árbol | `pyproject.toml` |
| T-11 | Parcial | `local/xedit/` | Retirar BLE001 incrementalmente del resto del árbol | `pyproject.toml` |
| T-12 | Parcial | `local/fomod/`, `local/loot/` | Migrar los módulos aún exentos a mypy estricto | `pyproject.toml` |
| T-16c | Cerrado | #288, #306, #323 | — | tests de preflight por ritual |
| T-22 | Parcial | #522 para `prefers-reduced-motion` | Revisar/cerrar el resto del contrato de transiciones | `test_gui_theme_contracts.py` y humano |
| T-23 | Abierto | — | Virtualizar listas grandes de mods | humano |
| T-24 | Parcial | #522 para foco visible de teclado | Inventario exhaustivo de labels y formularios accesibles | `test_gui_theme_contracts.py` y humano |
| T-25 | Parcial | — | Cerrar T-27 y después ejecutar la matriz E2E con Skyrim, MO2 y herramientas reales | `TECHNICAL_REVIEW_TASKS.md:247-249`; humano |
| T-26 | Cerrado | #309, #318 y cierres posteriores | — | productores de `persist_action_manifest` |
| T-27 | Parcial | ADR 0005 para Synthesis y salida administrada de Pandora | Migrar Pandora, DynDOLOD y Wrye Bash al flujo aislado USVFS con diff/promoción | código, ADR 0005 y `test_pandora_service.py` |
| T-28 | Cerrado | #309, #318 y cierres posteriores | — | productores de `persist_flight_report` |
| T-29 | Cerrado | Oleada 7 | — | historial Git y tests |
| T-30 | Cerrado | Oleada 7 | — | historial Git y tests |
| T-31 | Cerrado | #436 — lock cross-process por `install_dir`/`mod_dir` en `ToolsInstaller` (8 rutas de instalación incl. `ensure_skse` + wiring en `app_context.py`; ancla de familia e introspección) | — | `test_tools_installer_lock_scoping.py`, `test_tools_installer_lock_contencion.py` |
| U-01 | Cerrado | #381, #388 y documentación operativa | — | `test_output_targets.py` |
| U-02 | Cerrado | #360 | — | tests de Job Object |
| U-03 | Cerrado | #356 | — | tests de reconciliación de precache |
| U-04 | Parcial | #397, #399, salida administrada de Pandora y subárbol administrado por grupo de BodySlide | Smoke real de rollback de Pandora | `test_rollback_salida.py`, `test_pandora_service.py`, `test_bodyslide_lock.py` y humano |
| U-05 | Cerrado | #354 | — | `test_vramr_service.py` |
| U-06 | Parcial | #375 para DynDOLOD; post-check de artefacto de BodySlide; veredicto por `Engine.log` de Pandora; post-check por LOG **+ gate de frescura** de DynDOLOD/TexGen (binarios GUI sin stdout: ni el exit code ni un artefacto sin fecha alcanzan — el staging no se limpia entre corridas, así que la salida vieja satisfacía el gate) | Post-check de Wrye Bash; criterio seguro para QuickAutoClean; **completitud** de la etapa 9: el gate prueba que el artefacto cambió, no que la corrida terminó. el rig 2026-08-10 midió que DynDOLOD persiste el `.esp` al final (t3 > t4), lo que abre la puerta a apretar el criterio con marcadores de completitud del log. **Esa medición no es auditable desde el árbol** — informe fuera del repo, una sola corrida — así que la fila NO la da por cerrada: es la premisa que T2 tiene que re-verificar con la fixture del log commiteada, no un hecho establecido. Los marcadores **ya están cableados** en los dos lanzadores y TexGen tiene el suyo (`TexGen completed successfully`): ver la fila «Clasificación del log de la etapa 9» para el estado vigente, que es donde vive ese contrato | tests de cada runner, `test_bodyslide_postcheck_artefacto.py`, `test_pandora_runner.py`, `test_dyndolod_service.py` (post-check por log, frescura y ancla enumerativa de la familia) |
| U-07 | Cerrado | #355 | — | tests de Job Object de DynDOLOD |
| U-08 | Cerrado | #378 y reconciliador de arranque | — | `test_rollback_reconciler.py` |
| U-09 | Cerrado | #376 | — | tests del journal de grass |
| U-10 | Cerrado | #357 | — | tests de timeouts dedicados |
| U-11 | Cerrado | #359 | — | tests de `_process.run_capture` |
| U-12 | Cerrado | #358 | — | tests de limpieza del script xEdit |
| F1 | Cerrado | #328 | — | auditoría de resiliencia #319 |
| F2 | Cerrado | #320 y #322 | — | auditoría de resiliencia #319 |
| F3 | Cerrado | #321 | — | auditoría de resiliencia #319 |
| F4 | Cerrado | #342 | — | auditoría de resiliencia #319 |
| F5 | Cerrado | #332 | — | auditoría de resiliencia #319 |
| F6 | Cerrado | #331 | — | auditoría de resiliencia #319 |
| F7 | Cerrado | #343 | — | auditoría de resiliencia #319 |
| F8 | Cerrado | #343 | — | auditoría de resiliencia #319 |
| F9 | Parcial | #518, #520, #524, #526/#527, #540, #545 | Extraídos los seams de dominio PR1–PR6 del composition root de `SupervisorAgent` (dispatcher deps tipadas, composición de servicios, grass runtime deps, asset conflict scan, plugin-limit y —desde #545 mergeado— `RecordConflictScanner` para `scan_record_conflicts`); la generación de Wrye Bash también quedó extraída a `WryeBashPipelineService`. De las fachadas que restan en el supervisor hay que distinguir dos grupos, verificados contra el código: (a) `asset_detector` y `scan_record_conflicts` tienen callers productivos reales — la GUI accede a `runtime.supervisor.asset_detector` (`sky_claw/app/gui/sky_claw_gui.py:808`) y llama `runtime.supervisor.scan_record_conflicts()` (`sky_claw/app/gui/sky_claw_gui.py:860`); (b) `scan_asset_conflicts`/`scan_asset_conflicts_json` y `execute_wrye_bash_pipeline` no tienen caller productivo por la fachada — el path productivo del dispatcher se cablea directo desde los seams extraídos (`asset_conflict_scanner.scan`/`.scan_json` en `supervisor.py`; `build_wrye_bash_pipeline(service=...)` en la composition root) y esas fachadas se conservan sobre todo para que tests/harness BDD las monkeypatcheen con late-binding (`supervisor.py:537`). En ambos grupos las fachadas delegan en los seams y no requieren extracción. Falta el cierre del composition root sobre las responsabilidades de lifecycle/UI/rollback/HITL, que la auditoría #319 cedió al trabajo de lifecycle y quedan fuera del roadmap de seams de dominio | `test_dispatcher_dependencies.py`, `test_orchestration_composition.py`, `test_grass_runtime_deps_provider.py`, `test_asset_conflict_scan.py`, `test_plugin_limit_guard.py`, `test_record_conflict_scan_wiring.py`, `test_scan_record_conflicts.py` |
| F8 USVFS | Parcial | ADR 0007 y smoke `vfs-health` | Migrar runners restantes y completar smokes reales de cancelación, perfil y rollback | historial OODA, addendum F8 USVFS |
| Detección de enlaces | Cerrado | #404 y #405 | — | `test_links.py` |
| Borrado recursivo | Cerrado | #405 y #416 | — | `test_borrado_recursivo.py` |
| Medición de árboles | Cerrado | #416 | — | `test_borrado_recursivo.py` |
| Fugas de lifecycle en tests | Cerrado | #408, #409 y #415 | — | `test_atribucion_de_warnings.py`, `test_project_config.py` |
| Contrato de argumentos CLI | Parcial | LOOT (×2), BodySlide, Pandora, Wrye Bash, DynDOLOD, xEdit | Verificar contra fuente Synthesis, MO2 y VRAMr; re-verificar Pandora contra el binario 4.3.1-beta pinneado (el README de `main` puede no describirlo) | `test_contrato_argumentos_cli.py` |
| Contrato de veredicto de éxito | Parcial | Veredicto por `Engine.log` de Pandora; cruce con errores parseados en el call site de xEdit; ancla que enumera | Verificar contra rig real qué severidad usa Pandora para la reversión de un nodo inválido — ver nota abajo | `test_contrato_veredicto_de_exito.py` (inventario congelado en vacío), `test_pandora_runner.py`, `test_xedit_service.py` |
| Etapa 6 (Wrye Bash) sin build headless | Abierto | — | Decidir entre etapa asistida o retirarla del dispatcher | `test_contrato_argumentos_cli.py`; humano; auditoría 2026-08-04 |
| Orden de masters sin validar | Cerrado | `master_order.py`, cableado en Wrye Bash / Synthesis / DynDOLOD | — | `test_master_order.py` (ancla de cableado); auditoría 2026-08-04 (V-7) |
| Segundo parser TES4 sin gate | Cerrado | `test_tes4_parser_invariant.py` | — | auditoría 2026-08-04 |
| MO2 executable path != MO2 instance base_directory | Cerrado | Resolución de `mods/` desde la metadata de la instancia (`ModOrganizer.ini` `[Settings] base_directory`/`mod_directory`, contrato PathSettings de MO2 verificado contra `src/settings.cpp`): `get_mo2_mods_path()` pasa de derivar `<exe>/mods` a la precedencia `MO2_MODS_PATH` → metadata de instancia (portable junto al exe, global bajo `%LOCALAPPDATA%\ModOrganizer`, o deferencia por `MO2_PATH` explícito de datos) → legacy portable → fail-closed; misma fuente para `resolve_modlist_path` (sin split-brain); path final exige `is_dir()` (rechaza archivo). **Wiring de producción cerrado**: `AppContext._construir_raices_sandbox` registra la base de instancia (vía `_raiz_datos_instancia_para_sandbox`, trust boundary explícito: canonicalizar, excluir drive root, exigir `is_dir`); `PathValidator` resultante permite que `get_mo2_mods_path`/`resolve_modlist_path` operen. La detección del ejecutable (`detect_mo2_path`/`AutoDetector`) y los constructores manuales `<raíz>/mods` de otras superficies siguen enumerados como deuda hermanada en `tests/test_path_resolution_service.py` (ancla) | — | `test_path_resolution_service.py` (incluye `TestWiringProductivoAppContext`) |
| Smoke real de QuickAutoClean | Bloqueado (rig humano) | — | Ejecutar SSEEdit QuickAutoClean en una instalación real | humano |
| Smokes reales restantes | Bloqueado (rig humano) | — | NGIO, scripts `.pas`, GUI/FOMOD y Telegram end-to-end | humano |
| Residuos OODA de bajo valor | Abierto | — | Solo retomar agrupados si cambia su relación esfuerzo/impacto | historial OODA §3 |
| Residuos de crash logging | Abierto | #383 cerró F1/F2 reales | Cinco deudas sin víctima productiva actual | historial OODA, addendum #372 |
| Preset de TexGen desvía `OutputPath` | Abierto | — | Determinar la precedencia `preset` vs `-o:` y decidir el mecanismo (limpiar/aislar, sembrar preset administrado, detectar en preflight o verificar el campo antes de Start) — ver sección propia abajo | rig T5 2026-08-11 (`INFORME_T5_ARGV_DYNDOLOD_ALPHA209.md` §7.3 — informe fuera del repo, ver la sección propia abajo) |
| Clasificación del log de la etapa 9 | Parcial | T2: taxonomía de tres cajas (`MARCADORES_DE_COMPLETITUD`, `NO_FATALES_DEL_DOMINIO`, `PATRONES_TERMINALES` + `clasificar_log`), veredicto `rc == 0` **AND** artefacto fresco **AND** completitud **AND** sin terminales en los DOS lanzadores, decodificación utf-8→cp1252→`replace`, y SOP §2.9 punto 3 enmendado (el log ausente pasa de warning a fallo). **La taxonomía del veredicto entera —completitud, terminales y warnings/no-fatales— se evalúa sobre la evidencia atribuible a ESTA corrida, y la atribución se DEMUESTRA:** el log se firma antes de lanzar con `(tamaño, sha256 del contenido)`, después de la corrida se re-firma el prefijo, y sólo si sigue intacto se clasifican los bytes que la corrida agregó, así que ni un marcador ni un terminal heredados influyen el veredicto cuando el binario apendea (dyndolod.info: el log normal apendea entre sesiones; el debug log es por-sesión); y si la frontera temporal es indeterminada (el log no se pudo sondear antes o después), la corrida falla por ese motivo sin atribuirle ninguna línea histórica | **Volcar el log real completo contra la taxonomía.** Las fixtures se armaron desde las CATEGORÍAS del §11.4 del informe, no desde las 121 ocurrencias: si alguna cae fuera de la lista de no-fatales, la regla fail-closed la clasifica terminal y una corrida buena sale roja. Requiere el log de rig, que no está en el árbol. Los dos residuales de la firma mtime+tamaño quedaron CERRADOS: (a) mtime sin crecer y (b) reemplazo que termina más grande ya no se toman por evidencia de esta corrida, porque la firma previa incluye el digest sha256 del contenido y el prefijo se re-firma después de la corrida — el recorte sólo procede con el append DEMOSTRADO, y si no se puede demostrar la corrida falla cerrado. Lo único que sigue abierto de esta fila es el volcado del log de rig | `test_dyndolod_taxonomia_log.py` (dict congelado, tres cajas, caso aislado sin marcador, marcador rancio con y sin append + su contracara, `test_un_terminal_de_una_sesion_anterior_no_enrojece_esta_corrida` parametrizado TexGen+DynDOLOD, `test_con_frontera_indeterminada_no_se_atribuyen_terminales_historicos`, los dos encodings y el byte que ningún codec acepta), `test_dyndolod_service.py` (gate de terminales aislado por lanzador); rig 2026-08-10 (informe fuera del repo) |
| Candidato de salida de TexGen | Cerrado | `DynDOLODRunner.TEXGEN_OUTPUT_NAME = "textures"`; preflight, manifest, frescura y packaging comparten `root/textures`; el mod conserva `textures/` como raíz Data-relative | —; el desvío por `OutputPath=` del preset sigue abierto en la fila anterior (T5) | `test_dyndolod_service.py`, `test_output_targets.py`; rig T5-B Alpha-209: escritura física observada en `<administered_root>/textures` (evidencia aportada para T3, informe fuera del repo) |
| Fronteras del handoff TexGen → DynDOLOD | Cerrado | Las TRES, fail-closed. El layout físico correcto destapó tres agujeros que el candidato roto contenía por accidente, y los tres se cierran negándose a afirmar lo que no se puede probar. **A — ownership:** la raíz administrada la comparten las dos herramientas, así que empaquetarla entera absorbía `root/textures` dentro de "DynDOLOD Output"; ahora la raíz NO es una unidad empaquetable (la detección sigue reconociéndola: se prohíbe el ownership, no el hallazgo), y la regla es abstracta —*namespace compartido ≠ namespace empaquetable*— no un filtro por el nombre `textures`. **B — propiedad:** el staging de TexGen entra al move-aside del servicio ANTES de lanzar, así que nace vacío y "lo que hay adentro" pasa a ser "lo que esta corrida generó"; la frescura era un predicado ∃ y nunca pudo ser ∀. El destino exacto quedó declarado en `rollback_reconciler` para que su residuo no quede huérfano tras una muerte dura. **C — visibilidad:** empaquetar en `mods/` no demuestra nada sobre lo que DynDOLOD lee, porque corre standalone contra el `-d:<Data>` físico; antes del spawn se exige que TODO el staging esté visible ahí con los mismos bytes (identidad física o sha256, sin muestreo), y si no, DynDOLOD no se lanza | **No se materializa nada automáticamente**: el gate mide y falla cerrado, la materialización sigue siendo del operador — pero desde el fix F1 el corte por visibilidad PRESERVA `mods/TexGen Output` (el resto de la corrida revierte igual), así que lo que hay que desplegar sobrevive y la continuación con `run_texgen=False` lo verifica byte a byte contra el `Data` antes de lanzar DynDOLOD. Antes el rollback borraba justo ese artefacto y el default de la GUI quedaba sin salida. La TX queda PENDIENTE, nunca ROLLED_BACK, y el registro nombra el directorio vivo. `mutation_coverage_complete` sigue en `False` (el dir del exe y el temp quedan fuera del move-aside). Los subroots exclusivos por herramienta —el fix definitivo de A, que elimina la clase en vez de la instancia— siguen ABIERTOS y bloqueados por el gate de rig de `sky_claw/local/AGENTS.md` §1, porque cambian el `-o:` | `test_dyndolod_service.py` (T-A + ancla de política, T-B, T-B-rollback ×2, T-C, T-C-positive, T-C-stale), `test_rollback_reconciler.py` (destino exacto), `test_borrado_recursivo.py` (política de medición del gate) |

<!-- markdownlint-enable MD013 -->

## TexGen: preset persistido puede desviar `OutputPath` fuera del staging administrado

> **Estado:** `OPEN / FOLLOW-UP — evidencia dinámica confirmada, fix no diseñado
> todavía`. **Prioridad sugerida:** P1, antes de automatizar completamente la GUI
> de TexGen.
>
> **Nota de nomenclatura:** el informe de rig lo rotula **F1**. En este inventario
> `F1` ya está tomado (fila `F1 | Cerrado | #328`, auditoría de resiliencia #319),
> así que acá lleva ID descriptivo. Al citarlo, referenciar el informe y no el
> número suelto.

### Evidencia

Medido sobre **Alpha-209 real** (rig 2026-08-11). **Evidencia externa, no
committeada a este repo**: el informe `INFORME_T5_ARGV_DYNDOLOD_ALPHA209.md`
(§6.1 y §7.3, citado abajo) y los dumps UIA que respaldan las afirmaciones sobre
el campo Output viven en la máquina del rig operador (`_rig_test\evidence\`),
no en el árbol de Sky-Claw. Un maintainer no puede auditarlos ni reproducirlos
directamente desde este repo; quien retome este ítem debería pedir el informe
o reproducir la corrida en su propio rig antes de tomar decisiones de diseño
apoyado solo en esta descripción (hallazgo del revisor Codex en el PR #463).

- el preset persistido `Edit Scripts\DynDOLOD\Presets\DynDOLOD_SSE_TexGen.ini`
  guarda una clave `OutputPath=` con el root de la corrida anterior;
- Sky-Claw pasó `-o:<root administrado>` correctamente, y el **argv se parseó
  bien**: el encabezado del log del binario ecoó `Using Output Path:` igual al
  root del argv;
- pero la GUI **pre-cargó el campo Output desde el preset**, no desde el argv
  (verificado por dump UIA del campo antes de pulsar Start);
- al pulsar Start, las **escrituras reales fueron al path del preset**, no al root
  administrado. Exit 0.
- quitando/aislando el preset, TexGen volvió a usar el root esperado (§6.3: campo
  GUI = valor del argv, escrituras en el root del argv, `Using Output Path:`
  exacto).

DynDOLOD no exhibió el comportamiento en el mismo rig, pero **no se probó con un
preset de DynDOLOD rancio**: la corrida fue sin preset. La asimetría no está
verificada, solo no observada.

### Riesgo

Sky-Claw puede **creer que controla la superficie de salida mediante `-o:`**
mientras TexGen escribe fuera del staging administrado. El log no delata la
divergencia: el encabezado sigue ecoando el argv.

**Lo que el gate de frescura de Sky-Claw SÍ contiene** — verificado leyendo
`_post_check` (`dyndolod_runner.py:1143`), `_tiene_artefacto` (`:1223`) y el
gate de empaquetado en `run_full_pipeline` (`:870`, `if texgen_result.success
and texgen_result.output_path`): el candidato administrado
(`root/textures`) se firma antes y después de lanzar, y si las escrituras
reales van al path del preset en vez de ahí, esa firma no cambia. El
post-check lo marca "artefacto rancio" → `success=False`, y **el empaquetado de
TexGen** no corre. Por diseño, esto descarta dos consecuencias que una lectura
apresurada del hallazgo sugeriría: un stale output **no puede** pasar por
fresco, y Sky-Claw **no puede** empaquetar a MO2 una superficie distinta de la
generada — la corrida falla cerrada, no en silencio (hallazgo del revisor Codex
en el PR #463, verificado contra el código antes de aceptarlo — no se listan
acá como riesgos).

**Precisión sobre el alcance de esa contención** (revisor Codex, PR #485,
verificado contra el código): el `_package_output_as_mod` que no corre es el de
TexGen y **solo ese**. `run_full_pipeline` no corta ahí — sigue a
`run_dyndolod` incondicionalmente (`dyndolod_runner.py:1088`) y, si DynDOLOD
sale bien, **sí empaqueta su salida a `mods/`** (`:1094-1100`). El pipeline
igual reporta `success=False` porque la fórmula exige `texgen_mod_path` cuando
`run_texgen` (`:1160-1167`), pero para entonces ya hay un mod escrito en
`mods/`, y retirarlo depende del rollback del servicio, no de este gate. La
redacción anterior decía "`_package_output_as_mod` nunca corre", que es falso
como enunciado general: hay **dos** call sites de empaquetado y toda tarea que
razone sobre esta contención tiene que trazar los dos.

Consecuencias **derivadas, no reproducidas** en el rig, que el gate de
frescura **no** cubre porque ocurren en el path del preset — fuera de
cualquier superficie que Sky-Claw mire:

- las escrituras reales quedan **fuera del alcance del rollback**: nada en
  `_dir_rollback.py`/`DirectoryRollback` conoce ese path, así que un
  cancel/timeout no las revierte ni las limpia;
- si el path del preset coincide con el staging de una corrida **de Sky-Claw**
  anterior (un root administrado viejo que el operador no limpió), las
  escrituras nuevas se mezclan ahí sin que Sky-Claw se entere — contaminación
  invisible de un árbol que Sky-Claw sí cree gestionar, aunque en ese momento
  no lo esté mirando;
- el mensaje de error que ve el operador ("El artefacto de TexGen no se
  regeneró durante la corrida") es técnicamente correcto pero engañoso: TexGen
  sí generó texturas, solo que en otro lugar. Es un problema de
  diagnóstico/UX, no de integridad.

### Investigación previa al fix

Antes de implementar nada:

1. determinar el **contrato exacto de precedencia** entre el `OutputPath` del
   preset y el `-o:` del argv (¿el argv solo alimenta el encabezado del log y la
   GUI gana siempre? ¿depende de si el preset existe?);
2. decidir el mecanismo entre: limpiar/aislar el preset, sembrar un preset
   administrado, detectar el conflicto en preflight, o verificar el campo Output
   antes de Start;
3. **preservar backup/restore del preset** si se decide modificarlo;
4. **no tocar presets del usuario en silencio** — es estado suyo;
5. agregar test o rig que demuestre que **TexGen Y DynDOLOD** escriben dentro
   del staging administrado incluso cuando existe estado persistido previo en
   SU PROPIO preset. `Presets\DynDOLOD_SSE_Default.ini` persiste su propia
   clave `OutputPath` (mismo mecanismo `Save/LoadPreset`, verificado por
   análisis del binario en la investigación previa de este ítem) — mismo
   riesgo potencial, y el rig de este hallazgo no lo ejercitó: corrió DynDOLOD
   sin preset presente. Cerrar el ítem verificando solo TexGen y dejando a
   DynDOLOD con el mismo defecto sin probar es el patrón que `AGENTS.md`
   nombra como el más repetido del repo — arreglar un hermano y no al otro
   (hallazgo del revisor Codex en el PR #463).

### Qué NO es

No es un defecto del contrato de argv del PR #462: en ese mismo rig el `-o:` se
parseó exacto en los dos binarios y el acceptance gate de quoting quedó cerrado
(informe §7.1). No bloquea ese merge.

## 2.3 Zero-Trust — 2 ítems residuales

Persisten dos trabajos no urgentes heredados del inventario Zero-Trust:

1. migrar las lecturas centralizadas de `PathResolutionService` desde variables
   de entorno a `config.toml`;
2. consolidar secretos bajo una API única de `CredentialVault` con backend de
   keyring. El vault ya está conectado al `LLMRouter`, pero esa consolidación no
   existe todavía.

La escritura de una variable de entorno tras instalar una herramienta no
contradice por sí sola el primer punto: la deuda pendiente es la fuente
centralizada de lectura.

## Argumentos CLI verificados contra fuente

Cuatro runners salían a producción pasando flags que las herramientas **no
declaran**. Se verificó leyendo el parser de cada herramienta, no informes:

- **LOOT** (`src/gui/qt/main.cpp`) declara `--auto-sort`; el repo pasaba `--sort`
  desde **dos** lanzadores (`local/loot/cli.py` y `app/core/windows_interop.py`).
  `--auto-sort` exige que se pase también `--game` (los dos lanzadores lo hacen) y
  cierra la GUI al terminar si no hubo errores (`main_window.cpp`,
  `handlePluginsAutoSorted` → `on_actionQuit_triggered`), así que esperar la
  terminación del proceso es correcto para este runner.
- **Wrye Bash** (`Mopy/bash/barg.py`): `-b` es el switch `--backup` de settings
  (`store_true`, con destino en `-f`). **No existe flag de build headless**; el
  comando que usaba el repo ni siquiera parseaba, así que la etapa 6 fallaba con
  un exit code de `argparse` sin causa legible.
- **BodySlide** (`src/program/BodySlideApp.cpp`, descriptor en `BodySlideApp.h`)
  declara `gbuild`/`t` como nombres **cortos** de `wxCmdLineParser` (de ahí el
  guion simple); el repo pasaba `-b`/`-o`. El descriptor declara además `p`
  (preset) y `tri` (morfos), que quedaron sin usar en el fix del vector: sin
  `-tri`, `GroupBuild` pasa `cmdTri=false` y no se emiten los `.tri` que OBody NG
  / RaceMenu cargan en memoria — un build silenciosamente incompleto. Ambos se
  cablearon después, con `-tri` por defecto.
- **Pandora** (README, "Startup Arguments"): `--game` y `--auto` no existen; los
  documentados para correr desatendido son `--auto_run`/`--auto_close`.

Causa raíz común: ningún smoke con binario real, y tests que **muestreaban** un
flag en vez de enumerar el vector. El ancla `test_contrato_argumentos_cli.py`
congela el inventario de módulos que spawnean y exige procedencia declarada por
lanzador. Verificados contra fuente: xEdit (`xeInit.pas` — `-T:`/`-P:`, game
mode) y DynDOLOD (doc oficial `Command-Line-Argument` + doc del paquete
`DynDOLOD-Shortcut.txt` + strings del binario, y por efecto en rig 2026-08-05:
`Game Mode: SSE`, `Using Temp Path:`; la etapa 9 es asistida — sin modo
desatendido). Quedan marcados `SIN VERIFICAR` en esa tabla: Synthesis, MO2 y
VRAMr (scripts en Nexus).

`vramr_service.py` además no usa Job Object, a diferencia de DynDOLOD (U-07) y
grass: si VRAMr spawnea compresores externos, un timeout deja huérfanos.

Dos deudas que deja abiertas el fail-closed de la etapa 6, para no confundirlas
con el defecto ya cerrado:

- `WryeBashPipelineService.execute_pipeline` **no** cortocircuita: sigue corriendo
  el preflight, tomando los locks `bashed-patch` + `load-order` y persistiendo un
  `ActionManifest` que declara una mutación, y recién ahí el runner eleva
  `WryeBashHeadlessUnsupportedError` y la TX queda `rolled_back`. El resultado
  hacia el caller es honesto (`success=False` con la causa nombrada), pero cada
  invocación escribe una TX en la caja negra por un ritual que no puede correr y
  retiene los locks mientras tanto. Cortocircuitar antes del preflight es trivial;
  se dejó para la misma decisión que define la etapa 6 (asistida vs. retirarla del
  dispatcher), porque el resto del pipeline —snapshot, journal, rollback— es
  exactamente lo que hay que reusar si vuelve como etapa asistida.
- `--auto_run` de Pandora corre "using the same active mods as cached from the last
  successful run": en una instalación sin corrida previa exitosa no hay caché, y no
  está verificado en rig real qué hace el motor en ese caso. Es el escenario que
  cubriría el smoke pendiente de U-04.
- **Sintaxis CLI de Pandora y soporte por versión: sin verificar.** El vector
  actual está verificado contra el README de la rama `main` del repositorio
  upstream, pero el repo pinnea el binario **4.3.1-beta**, que es otra cosa. La
  wiki de STEP documenta una sintaxis **distinta** para el mismo motor
  (`-autorun`, `-autoclose`, `-tesv:{path}`: guion simple y dos puntos en vez de
  `--auto_run`/`--tesv <ruta>`) y afirma además que *"command line arguments are
  not supported on newer Pandora versions"*. No fue posible verificarlo desde el
  entorno de desarrollo (403 tanto en STEP como en la API de GitHub). Si la
  afirmación fuera cierta, la automatización degrada **en silencio**: Pandora
  abriría la GUI y esperaría, hasta el timeout de 300 s y el rollback
  consiguiente, sin ninguna señal de que la causa fue el vector de argumentos.
- **Severidad exacta de la reversión de un nodo inválido: sin verificar.** El
  veredicto de `pandora_runner.py` (contrato de veredicto de éxito) hace fallar
  la corrida ante `ERROR`/`FATAL` en `Engine.log`, dejando `WARN` afuera a
  propósito. La lectura más literal de la definición del README para `ERROR`
  (*"prevented [this] work from completing"*) es justamente la reversión de un
  nodo —el motor no completó ESE nodo— así que bajo esa lectura el veredicto SÍ
  cierra el escenario que lo motiva (mods de animación descartados en
  silencio). Pero ninguna fuente cita explícitamente qué severidad usa la
  reversión, y si resultara ser `WARN` en vez de `ERROR`, este fix **no
  cerraría** el escenario original (hallazgo qodo, PR #439): una corrida que
  sólo revierte nodos —sin ningún `ERROR`/`FATAL`— seguiría reportando
  `success=True` con los mods descartados igual. El smoke de rig real (ídem
  U-04) es lo único que puede confirmarlo; hasta entonces `WARN` se cuenta y se
  surface en `PandoraResult.warnings`, no decide el veredicto.
  El mismo smoke de rig de U-04 debería empezar por confirmar qué acepta el
  binario pinneado.
- **Ventana de lectura de 8 MiB, sólo el PRIMER tramo del apéndice: cerrado.**
  `_MAX_BYTES_DE_LECTURA` truncaba el escaneo a un `read()` único desde la
  marca; una corrida que registrara más de 8 MiB de mensajes `INFO` antes de un `ERROR`
  final lo perdía en silencio (hallazgo qodo, PR #439, insistido en una
  segunda ronda con un fix concreto). A diferencia de la severidad de `WARN` o
  la sintaxis CLI, esto NO dependía de ningún supuesto sobre el binario — era
  enteramente una limitación de la propia estrategia de lectura, así que se
  corrigió: `_escanear_severidades` (`pandora_runner.py`) recorre el tramo
  ENTERO en bloques de `_TAMANO_DE_BLOQUE` (1 MiB) sin cargarlo completo en
  memoria, cortando siempre en un salto de línea para no partir un match a la
  mitad. Sólo el RESULTADO retenido queda acotado (`_MAX_LINEAS_REPORTADAS`/
  `_MAX_BYTES_DE_COLA`), no la entrada escaneada. Test de regresión con >8 MiB
  de `INFO` antes de un `ERROR`, y de corrección de límite con el corte de
  bloque cayendo exactamente dentro de la línea que matchea
  (`test_pandora_runner.py`). La identidad del archivo (inode/device, cerrada
  en la ronda anterior de este mismo PR) sigue siendo el ejemplo hermano: una
  propiedad verificable sin depender de ningún supuesto sobre el binario.
  También cerrados en la misma ronda: el veredicto se combinaba de la
  PRIMERA candidata que crecía en vez de las DOS (`exe.parent` y `output`),
  dejando un `ERROR` real en la segunda sin escanear si la primera sólo tenía
  `INFO`/`WARN`; y una línea sin salto que empieza con `ERROR:` y supera
  `_MAX_BYTES_DE_LINEA_PENDIENTE` se descartaba sin clasificar.
- **Truncado IN PLACE (`open(ruta, "w")`), mismo inode: cerrado.** El chequeo
  de identidad (inode/device) sólo detecta un archivo RECREADO (borrado +
  creado, o rotado). Un truncado in-place —abrir en modo escritura sin
  truncar-y-recrear— conserva el MISMO inode en POSIX (verificado
  experimentalmente), así que ese chequeo no lo veía: si el contenido
  reescrito crecía más allá de la marca vieja con un `ERROR` en los primeros
  bytes, el escaneo arrancaba en un offset sin relación con el contenido real
  (hallazgo qodo, PR #439, quinta ronda). Igual que el resto de esta familia
  de fixes, no depende de ningún supuesto sobre CÓMO abre Pandora su log —
  es una propiedad general de cualquier mecanismo de truncado. Cerrado con
  una huella (`hashlib.sha256`) de una muestra acotada (64 KiB) del contenido
  justo antes de la marca: si esa ventana cambió entre la medición y la
  lectura, la candidata queda no atribuible, igual que un cambio de
  identidad. Test con `open("w")` real (no fabricado) reescribiendo contenido
  más grande con `ERROR` al principio.
- **Ambas rutas candidatas de `Engine.log` sin verificar: si las dos están
  mal, el mecanismo se apaga sin ninguna señal.** El breadcrumb de formato no
  reconocido (`sky_claw/local/tools/pandora_runner.py`, tarea del contrato de
  veredicto) sólo se emite cuando ALGUNA candidata existe y creció; si Pandora
  escribe el log en un tercer lugar (`Data`, `%TEMP%`, AppData), ninguna marca
  crece nunca y el veredicto cae al exit code en silencio absoluto —el mismo
  desenlace que el mecanismo tenía antes de este PR, sin regresión, pero
  tampoco con la mejora que promete (hallazgo qodo, PR #439). No se agregó un
  warning para "ninguna candidata creció" porque no hay forma de distinguir
  eso de una corrida sana que simplemente no logueó nada — sin un rig no se
  sabe si Pandora escribe algo en un run limpio, y advertir en ese caso podría
  ser ruido en el 100% de las corridas sanas. Confirmar la ruta real es parte
  del mismo smoke de U-04.
- **Ubicación de `Engine.log`: asumida, no verificada.** El veredicto por log de
  Pandora (`pandora_runner._leer_engine_log`) sondea `<dir del exe>/Engine.log` y
  `<Pandora_Output>/Engine.log`. Ninguna de las dos está confirmada contra una
  instalación real. La consecuencia de errarle está acotada por diseño —un log
  que no aparece deja el veredicto en el exit code, o sea el comportamiento
  previo, nunca un falso rojo—, pero mientras no se confirme, el mecanismo puede
  estar apagado en producción sin que nada lo indique. Va al smoke de U-04.

## Decide — recomendación de próximo frente

El frente de cierres mecánicos verificables íntegramente por un agente está
agotado. Lo pendiente se divide en cuatro clases:

1. **prueba humana en rig real:** T-25, QuickAutoClean y los demás smokes de
   herramientas. Pandora ya no tiene modo `Data` implícito en el comando de
   Sky-Claw; el rig debe demostrar que Pandora 4.3.1-beta respeta `--output` en
   éxito, error, timeout y cancelación;
2. **aislamiento pendiente:** T-27 sigue abierto hasta que Pandora, DynDOLOD y
   Wrye Bash lean y ejecuten dentro del sandbox USVFS con diff/promoción;
3. **deuda incremental no bloqueante:** T-10/T-11/T-12, F9 y los residuales de
   bajo valor;
4. **decisión de diseño pendiente, bloqueante solo para su propio alcance:**
   "Preset de TexGen desvía `OutputPath`" (arriba). No encaja en las otras
   tres — la evidencia dinámica ya existe (no es prueba de rig pendiente), no
   es aislamiento USVFS, y es P1, no deuda de bajo valor. Requiere primero
   decidir el contrato de precedencia preset/`-o:` y el mecanismo (limpiar,
   sembrar, preflight o verificar antes de Start) antes de que un agente
   pueda tocar código, y el criterio de cierre exige a DynDOLOD y TexGen por
   igual (ver "Investigación previa al fix" del ítem).

El próximo PR debería partir de evidencia nueva del rig, del aislamiento
pendiente. Este inventario no declara GA. No conviene abrir otra caza general de
hallazgos sobre este snapshot.

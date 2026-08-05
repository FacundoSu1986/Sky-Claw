# OODA — inventario vivo de pendientes

> **Audiencia:** maintainers, reviewers y agentes.
>
> **Estado:** Parcial.
>
> **Fuentes canónicas:** código y tests del árbol actual; ADRs aceptados para
> decisiones; este documento únicamente consolida su estado.
>
> **Última verificación:** 2026-08-02 en la rama actual, basada en
> `origin/main` `bf635e0`; los cambios posteriores de esta rama no se afirman
> integrados en `origin/main`.

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
| T-22 | Abierto | — | Transiciones y `prefers-reduced-motion` | humano |
| T-23 | Abierto | — | Virtualizar listas grandes de mods | humano |
| T-24 | Abierto | — | Labels y foco visible en formularios | humano |
| T-25 | Parcial | — | Cerrar T-27 y después ejecutar la matriz E2E con Skyrim, MO2 y herramientas reales | `TECHNICAL_REVIEW_TASKS.md:247-249`; humano |
| T-26 | Cerrado | #309, #318 y cierres posteriores | — | productores de `persist_action_manifest` |
| T-27 | Parcial | ADR 0005 para Synthesis y salida administrada de Pandora | Migrar Pandora, DynDOLOD y Wrye Bash al flujo aislado USVFS con diff/promoción | código, ADR 0005 y `test_pandora_service.py` |
| T-28 | Cerrado | #309, #318 y cierres posteriores | — | productores de `persist_flight_report` |
| T-29 | Cerrado | Oleada 7 | — | historial Git y tests |
| T-30 | Cerrado | Oleada 7 | — | historial Git y tests |
| T-31 | Abierto | — | Lock cross-process por `install_dir`/`mod_dir` en `ToolsInstaller` (6 rutas de instalación + wiring en `app_context.py`) | `TECHNICAL_REVIEW_TASKS.md` |
| U-01 | Cerrado | #381, #388 y documentación operativa | — | `test_output_targets.py` |
| U-02 | Cerrado | #360 | — | tests de Job Object |
| U-03 | Cerrado | #356 | — | tests de reconciliación de precache |
| U-04 | Parcial | #397, #399, salida administrada de Pandora y subárbol administrado por grupo de BodySlide | Smoke real de rollback de Pandora | `test_rollback_salida.py`, `test_pandora_service.py`, `test_bodyslide_lock.py` y humano |
| U-05 | Cerrado | #354 | — | `test_vramr_service.py` |
| U-06 | Parcial | #375 para DynDOLOD; post-check de artefacto de BodySlide; veredicto por `Engine.log` de Pandora | Post-check de Wrye Bash; criterio seguro para QuickAutoClean | tests de cada runner, `test_bodyslide_postcheck_artefacto.py`, `test_pandora_runner.py` |
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
| F9 | Abierto | — | Reducir el composition root de `SupervisorAgent` incrementalmente | auditoría de resiliencia #319 |
| F8 USVFS | Parcial | ADR 0007 y smoke `vfs-health` | Migrar runners restantes y completar smokes reales de cancelación, perfil y rollback | historial OODA, addendum F8 USVFS |
| Detección de enlaces | Cerrado | #404 y #405 | — | `test_links.py` |
| Borrado recursivo | Cerrado | #405 y #416 | — | `test_borrado_recursivo.py` |
| Medición de árboles | Cerrado | #416 | — | `test_borrado_recursivo.py` |
| Fugas de lifecycle en tests | Cerrado | #408, #409 y #415 | — | `test_atribucion_de_warnings.py`, `test_project_config.py` |
| Contrato de argumentos CLI | Parcial | LOOT (×2), BodySlide, Pandora, Wrye Bash | Verificar contra fuente xEdit, Synthesis, MO2, DynDOLOD y VRAMr; re-verificar Pandora contra el binario 4.3.1-beta pinneado (el README de `main` puede no describirlo) | `test_contrato_argumentos_cli.py` |
| Contrato de veredicto de éxito | Parcial | Veredicto por `Engine.log` de Pandora; cruce con errores parseados en el call site de xEdit; ancla que enumera | Verificar contra rig real qué severidad usa Pandora para la reversión de un nodo inválido — ver nota abajo | `test_contrato_veredicto_de_exito.py` (inventario congelado en vacío), `test_pandora_runner.py`, `test_xedit_service.py` |
| Etapa 6 (Wrye Bash) sin build headless | Abierto | — | Decidir entre etapa asistida o retirarla del dispatcher | `test_contrato_argumentos_cli.py`; humano; auditoría 2026-08-04 |
| Orden de masters sin validar | Cerrado | `master_order.py`, cableado en Wrye Bash / Synthesis / DynDOLOD | — | `test_master_order.py` (ancla de cableado); auditoría 2026-08-04 (V-7) |
| Segundo parser TES4 sin gate | Cerrado | `test_tes4_parser_invariant.py` | — | auditoría 2026-08-04 |
| Smoke real de QuickAutoClean | Bloqueado (rig humano) | — | Ejecutar SSEEdit QuickAutoClean en una instalación real | humano |
| Smokes reales restantes | Bloqueado (rig humano) | — | NGIO, scripts `.pas`, GUI/FOMOD y Telegram end-to-end | humano |
| Residuos OODA de bajo valor | Abierto | — | Solo retomar agrupados si cambia su relación esfuerzo/impacto | historial OODA §3 |
| Residuos de crash logging | Abierto | #383 cerró F1/F2 reales | Cinco deudas sin víctima productiva actual | historial OODA, addendum #372 |

<!-- markdownlint-enable MD013 -->

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
lanzador. Quedan marcados `SIN VERIFICAR` en esa tabla: xEdit, Synthesis, MO2,
DynDOLOD (binario cerrado, sin fuente pública) y VRAMr (scripts en Nexus).

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
- **Ubicación de `Engine.log`: asumida, no verificada.** El veredicto por log de
  Pandora (`pandora_runner._leer_engine_log`) sondea `<dir del exe>/Engine.log` y
  `<Pandora_Output>/Engine.log`. Ninguna de las dos está confirmada contra una
  instalación real. La consecuencia de errarle está acotada por diseño —un log
  que no aparece deja el veredicto en el exit code, o sea el comportamiento
  previo, nunca un falso rojo—, pero mientras no se confirme, el mecanismo puede
  estar apagado en producción sin que nada lo indique. Va al smoke de U-04.

## Decide — recomendación de próximo frente

El frente de cierres mecánicos verificables íntegramente por un agente está
agotado. Lo pendiente se divide en tres clases:

1. **prueba humana en rig real:** T-25, QuickAutoClean y los demás smokes de
   herramientas. Pandora ya no tiene modo `Data` implícito en el comando de
   Sky-Claw; el rig debe demostrar que Pandora 4.3.1-beta respeta `--output` en
   éxito, error, timeout y cancelación;
2. **aislamiento pendiente:** T-27 sigue abierto hasta que Pandora, DynDOLOD y
   Wrye Bash lean y ejecuten dentro del sandbox USVFS con diff/promoción;
3. **deuda incremental no bloqueante:** T-10/T-11/T-12, F9 y los residuales de
   bajo valor.

El próximo PR debería partir de evidencia nueva del rig o del aislamiento
pendiente. Este inventario no declara GA. No conviene abrir otra caza general de
hallazgos sobre este snapshot.

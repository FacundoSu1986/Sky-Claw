# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed
- **Una etapa 9 a la vez para la GUI y el agente (seguimiento de #1007).** La GUI y el agente (Telegram, `/api/chat`) tenían cada uno su control de «una corrida a la vez» y ninguno veía al otro: si se lanzaba la etapa 9 desde los dos, la segunda corrida le borraba a la primera el paso actual, el corte y el progreso, y la guía en criollo de un fallo podía perder la explicación específica. Ahora el turno lo toma `DynDOLODPipelineService` en el límite de `execute`, sin `await` entre mirar y tomar: una segunda corrida se rechaza con `reason="EtapaEnCurso"` y una guía («Ya hay una etapa 9 corriendo.») sin tocar el estado de la dueña, y las dos superficies lo consultan antes de pedir aprobación o crear el trabajo. El `dry_run` no ocupa el turno. Ancla: `tests/test_dyndolod_una_etapa9_a_la_vez.py` (las cuatro combinaciones GUI/agente, la autoridad del servicio ante una llamada directa, la liberación del turno ante una cancelación y el orden por AST en cada lanzador de la familia).

### Added
- **Grass cache NGIO: el arranque ya no puede borrar el caché (P0 SAFE CACHE, F-01).** El runner escribía `PrecacheGrass.txt` vacío en *todas* las invocaciones, y NGIO-NG borra `*.cgid`/`*.dgid`/`*.fail` de `Data/Grass` cuando ese archivo no tiene progreso (verificado en `src/GidFileCache.cpp::GidFileGenerationTask::Init()` del tag `1.0.13`: líneas leídas con `trim`, blancas descartadas, `IsResuming = !ProgressDone->empty()`, y `remove()` de las tres extensiones si queda vacío). Un precache nuevo, incluso uno que sólo quería reanudar, podía borrar horas de trabajo. Ahora hay tres modos con precondiciones verificables en `sky_claw/local/tools/grass_cache_safety.py` (módulo nuevo, mypy estricto, única pieza que escribe el flag): `new` (default) exige caché vacío y sin progreso; `resume` nunca trunca el flag —conserva el progreso **byte a byte** o restaura el archivado (base64 + sha256) si la huella del caché coincide—; `rebuild` exige aprobación literal `REBUILD` **y** un respaldo verificado **por contenido**. El servicio corre el mismo `preparar_arranque` **antes de clonar el perfil** y el runner lo repite sobre el inventario fresco, así no hay dos políticas que puedan divergir; un rechazo no abre transacción de journal ni toca el árbol MO2. Al salir, el flag se neutraliza (el juego del usuario no puede quedar en modo precache) pero su contenido se archiva antes. **Revisión del propietario aplicada (`P0_CHANGES_REQUESTED`):** (1) el respaldo ya no se reusa por metadatos —exige que el manifiesto coincida con el **contenido actual** del origen y con los **bytes actuales** del destino, y se re-verifica otra vez justo antes de escribir el flag vacío—, con lo que un caché del mismo tamaño pero distinto contenido, o un respaldo alterado conservando el tamaño, ya no habilitan un borrado; (2) la raíz del caché y todos sus componentes se validan con `exigir_contencion_fisica` (symlinks **y** junctions de Windows), no sólo los archivos individuales; (3) el progreso se lee, archiva y restaura como bytes, sin decodificar con `errors="replace"`; (4) un `unlink` que falla ya no se reporta como limpieza exitosa (`flag_residual` + errores en el resultado y en el journal, y el `finally` sólo toca un flag que el run adoptó). **Cobertura USVFS: HOLD explícito.** El respaldo cubre `overwrite/Grass`, pero NGIO borra `Data/Grass` a través de la VFS, que superpone el `Grass/` de los mods habilitados (el propio repo lo asume en `etapas_previas_lods.py`): un `.cgid` en un mod **no** está respaldado. Mientras esa semántica no se demuestre en un rig, **ningún** arranque que escriba el flag vacío (`new` **y** `rebuild`) arranca sin `ack_usvfs_no_verificado=True`; el reconocimiento se muestra en el prompt HITL y el plan declara `cobertura_usvfs: no_verificada`. `new` deja de presentarse como inofensivo porque el `remove()` de NGIO no distingue de dónde sale el `.cgid`. El resultado del runner y del servicio exponen `mode`, `backup_dir` y `flag_residual`. **Segunda revisión independiente (sobre el commit del parche):** (1) el servicio ya **no** declara éxito con un flag residual —un `unlink` fallido deja `success=False`, mensaje y `teardown_failures`, y la transacción del journal cierra como fallida—; (2) el temporal del flag es **impredecible y exclusivo** (`O_CREAT|O_EXCL|O_NOFOLLOW`, mismo directorio) y el flag se relee tras la sustitución: un symlink plantado en el nombre del temporal ya no redirige la escritura fuera del directorio del juego; (3) si la clasificación del flag falla, **no se borra** (fail-closed: "no sé qué es" no autoriza `unlink`); (4) borrar el flag sin poder archivar su progreso se declara `progreso_perdido` y sale de "limpieza exitosa", y el archivado previo a `rebuild` ya no es best-effort (`suppress(OSError)`): si falla, se niega el arranque; (5) la identidad del progreso deja de ser `(nombre, tamaño)` —se archiva y se compara también la huella de **contenido** del caché, y un `.cgid` que cambió de bytes conservando el tamaño ya no habilita la reanudación (`progreso_de_otro_cache`)—. **Tercera revisión independiente:** (1) la evidencia de progreso y el manifiesto del respaldo se escriben con el mismo temporal **exclusivo** que el flag —antes usaban `<huella>.json.tmp` y `.manifiesto.json.tmp`, nombres derivables sobre los que un enlace redirigía la escritura— y se valida la contención física de la raíz de estado (`estado_raiz_enlazada`) tanto al archivar como al recuperar; (2) RESUME **revalida el flag** (presencia, tipo y bytes exactos) al aplicarlo, así que un flag vaciado o borrado entre el preflight y el arranque falla cerrado (`flag_cambio_despues_del_preflight`) en vez de reanudar sin evidencia; (3) `aplicar_plan` informa `EstadoAplicacion(escrito, verificado)` y el runner **adopta** el flag en cuanto la sustitución ocurrió, aunque falle el postcheck: ya no puede quedar un flag vacío sin dueño; y `GrassCacheRunner.run()` declara `success=False` cuando la limpieza no fue completa. Se documenta expresamente que `ack_usvfs_no_verificado` es **aceptación de riesgo, no certificación de cobertura**, y que no se corren operaciones destructivas sobre cachés reales sin evidencia de rig o respaldo completo de los archivos alcanzables. **Tests:** `tests/test_grass_cache_safety.py` (nuevo) más casos en `tests/test_grass_cache_runner.py`, `tests/test_grass_cache_service.py` y `tests/test_grass_cache_strategy.py`; los tests que parten de un `.cgid` preexistente declaran `rebuild` con respaldo y ack en vez de heredar un default destructivo. **No verificado:** ningún smoke real (Windows/MO2/USVFS/SKSE/NGIO) y el binario de Nexus 2.2.0 no fue inspeccionado — el contrato es fail-closed respecto de su semántica, no una promesa de compatibilidad. **Cuarta revisión independiente:** (1) la contención física se valida sobre la cadena **completa del destino** del progreso —`<estado>/progreso/<huella>.json`— y no sólo sobre la raíz de estado: un enlace en el subdirectorio `progreso` (o en un ancestro intermedio, junction incluido) ya no permite escribir ni leer la evidencia fuera del árbol administrado, y el archivo de evidencia se clasifica aparte (`progreso_raiz_enlazada`/`progreso_es_enlace`); (2) la escritura del flag corre en una **sección crítica a prueba de cancelación**: si la cancelación se entrega mientras el hilo de `aplicar_plan` sigue en vuelo, el run espera a que ese hilo termine y recién entonces propaga, de modo que el estado del flag es conocido y el `finally` lo neutraliza en vez de dejar un flag vacío sin dueño. **Quinta revisión (CI roja + hilos pendientes):** (1) la CI de Windows quedó verde: tres tests del contrato escribían el flag con `Path.write_text()`, que en Windows traduce LF→CRLF, así que sus aserciones de bytes fallaban por el fixture y no por el código — ahora escriben los bytes exactos con `write_bytes()` y un ancla por AST impide que vuelva a entrar; (2) el flag ALTERNATIVO `PrecacheGrass.txt.txt`, que NGIO 1.0.13 usa cuando el primario no existe (`Util::getProgressFilePath`), se rechaza antes de escribir nada (`flag_alternativo_presente`): ignorarlo permitía enmascararlo con `new` y que reapareciera al neutralizar el primario; (3) el parser de progreso es ahora la réplica **literal** del lector de NGIO (`std::getline` + `trim` de " \r\v\n"), cerrando las divergencias de tab y form feed; (4) un respaldo que es enlace o **hardlink** del origen ya no verifica ni autoriza el borrado (`respaldo_enlazado`/`respaldo_aliaseado`); (5) la evidencia de progreso queda atada al **ámbito** —camino físico del caché + perfil— además del contenido, y la de otro origen se declara (`progreso_de_otra_instalacion`) en vez de restaurarse; (6) una cancelación durante la copia del respaldo ya no suelta los dos locks con el hilo escribiendo, y la limpieza fallida del flag se publica ANTES de propagar la cancelación. Reconciliación F-01…F-12, revisión del propietario y límites en `docs/audits/2026-11-23_ngio_p0_safe_cache.md`. **Sexta revisión (corrección adversarial final, F1–F6):** (1) la identidad del progreso deja de ser el perfil CLON —`SkyClaw-GrassCache`, constante— y pasa a ser el perfil de ORIGEN: dos perfiles de la misma instalación MO2 comparten `overwrite/Grass` y el mismo clon, así que el `resume` de uno podía restaurar el progreso del otro (otro conjunto de mods); `GrassCacheConfig` gana `progress_profile`/`perfil_de_progreso` como única fuente de verdad para archivar y restaurar, y `profile` sigue siendo el perfil de lanzamiento, de modo que `MO2Controller` no cambia; (2) la neutralización del flag ya no corre en el event loop: archiva y hashea miles de `.cgid/.dgid/.fail`, y con el loop bloqueado el heartbeat de los locks no renueva —medido con locks reales: lease VENCIDO (`expires_at` 0,5 s por detrás del reloj) mientras el run seguía mutando y sin que `lease_lost` se enterara—; ahora va en `asyncio.to_thread` **y** se espera con `esperar_terminar`, así que sale del loop sin reabrir B11 (esto revierte el aplazamiento de la fila D de §13 de la auditoría, cuya objeción valía para un `await` sin drenar); (3) el test de cancelación durante la escritura del flag escribía el flag ANTES de la barrera y por eso no discriminaba el mutante M4 —ahora la barrera va antes y el mutante muere—; (4) `recuperar_progreso()` usa el predicado de NGIO (`trim` sin tab ni form feed) en vez de `str.strip()`, que declaraba vacío un progreso válido y empujaba al `rebuild` destructivo; (5) `_digests_del_cache()` clasifica el enlace ANTES de `is_file()`, que sigue enlaces y descartaba en silencio un `.cgid` enlazado o roto (fail-open) que el borrado de NGIO sí atraviesa; (6) `respaldar_cache()` se niega a usar un respaldo que se solape con el caché (mismo directorio, dentro o por encima: la rotación movía el original antes de copiar) y la cuarentena de un respaldo inválido usa un nombre único (colisión a segundo resuelto + `suppress(OSError)` antes), sin pisar ni borrar nada. Tests nuevos en `tests/test_grass_cache_{runner,safety,service}.py`; mutantes M4, M-F1 y M-F2 verificados. Límites en pie: NGIO 2.2.0 y la cobertura USVFS siguen **NO VERIFICADOS**. **Segunda tanda de la ronda 8 (revisor automático sobre `de481b77`):** (1) el resultado de la neutralización se registra **dentro** del trabajo drenado: con la asignación afuera, una cancelación durante la neutralización la salteaba y un flag que no se pudo borrar quedaba sin reportar (regresión introducida por el arreglo de F2); ahora además se loguea como warning y el runner expone `neutralizacion`; (2) `respaldar_cache` compara la huella `(nombre, tamaño)` del origen contra la del inventario (`respaldo_de_otro_conjunto`): un renombrado conservaba conteo y bytes y el manifiesto quedaba identificando un conjunto ajeno; (3) corregida la documentación de los tres modos (el `ack` de `new` aplica también con el caché vacío; `rebuild` pide respaldo sólo si hay caché y raíz de estado sólo si hay progreso vivo) y del costo de hashing (`resume` con restauración y la neutralización de salida también hashean el caché). Registrados como **P2** sin implementar: la verificación del contenido del flag antes de cada relanzamiento de `resume` y las claves de progreso/respaldo que no incluyen el contenido del caché (detalle en §15 de la auditoría).
- **El modo asistido de la etapa 9 guía al operador: checklist antes de arrancar, «qué hacer ahora» durante la corrida y cortes explicados en criollo (#997).** R0 brokered 2 dio PASS pero necesitó ocho intentos: casi todos los cortes fueron precondiciones que nadie avisó o un orden de pasos que el operador no tenía forma de saber, reportados después de fallar y en jerga.
  - **Checklist compartido** (`sky_claw/local/tools/dyndolod_guia.py`, mypy estricto): DynDOLOD/TexGen encontrados, en una carpeta que el sandbox admite (dice cuáles), fuera de la carpeta del juego, salida vieja de DynDOLOD desactivada (`DynDOLOD.esp`/`Occlusion.esp`), etapas previas, `OutputPath` del preset contra el `-o:` y DynDOLOD DLL NG con masters sin limpiar. Cada ítem trae título y arreglo en una línea y el detalle técnico aparte. ❌ solo para fallas seguras o de seguridad y **sin override** (decisión del propietario); lo incierto es ⚠️ y no frena.
  - **Una autoridad, todas las superficies:** `DynDOLODPipelineService.execute` corre el checklist antes de cualquier efecto (preflight, evento, runner, lock) y corta con `reason="ChecklistBloqueado"`. La GUI (`GenerateLodsStrategy`, antes de pedir la aprobación) y el agente (`GestorDeTrabajosDeLods`, antes de ocupar el lugar del trabajo) lo consultan antes de preguntar o lanzar, y devuelven el MISMO `checklist`/`guia`.
  - **Indicador de paso en vivo:** el runner avisa cada paso del protocolo de readiness asistido (abriendo la ventana → esperando tu aprobación → verificando el Output → «ya podés apretar Start/OK» → terminó) y el corte con su código tipado. La GUI lo muestra arriba del panel de la etapa 9 y `lods_status` lo devuelve como `fase`. Los botones (`Start`/`Exit TexGen`, `Advanced >>>`/`OK`/`Save and Exit`) salen del contrato de UI medido.
  - **Criollo primero:** todo resultado fallido de la etapa 9 trae `guia = {titulo, arreglo, detalle_tecnico}` (`message` sigue técnico); el toast muestra la guía sin tocar su vista (#996) y Telegram la pone antes del técnico. La tarjeta de DynDOLOD muestra «Antes de arrancar» con desplegables cerrados, frena el botón con un ❌ y ofrece «Usar Asistido» si en Desatendido faltan etapas previas. Recordatorio de la pestaña abierta (GUI) y del cierre a los 90 s (`SkyClawApp.bat`).
  - **Tests (anclas que enumeran):** `test_dyndolod_guia_catalogo.py` (catálogo, fases y explicaciones por igualdad literal y totalidad sobre los enums de corte), `test_dyndolod_guia_superficies.py` (familia de lanzadores por AST, paridad GUI/agente, compuerta antes de todo efecto, al runner solo se llega por el servicio), `test_dyndolod_guia_fases.py` (todo `raise` del protocolo asistido avisa su corte), `test_dyndolod_guia_chequeos.py`, `test_gui_guia_etapa9.py`. El agente ya no descarta `preflight` del resultado. **No verificado:** el render en un navegador real ni una corrida asistida real en Windows con MO2/USVFS.
- **CI: gate de merge «🚦 Main en verde».** Workflow propio `.github/workflows/main-en-verde.yml`: en cada PR contra `main` (incluidos `labeled`/`unlabeled`) el check falla si la última corrida TERMINADA de `ci.yml` sobre `main` (push, nocturna o a pedido) tiene en rojo alguno de los cuatro checks obligatorios; una corrida sin veredicto se saltea y, si ninguna lo tiene, falla cerrado. Al terminar cada corrida de CI sobre `main`, el job `reevaluar` vuelve a correr el gate de todos los PRs abiertos, así un PR que pasó antes de que `main` se rompiera no queda en verde; si GitHub ya no deja re-correrlo (30 días o 50 reruns), lo pasa a borrador con un comentario y al marcarlo listo el gate corre de cero. El PR que arregla `main` pasa con la etiqueta `ci:repara-main` (leída en vivo). Lógica en `scripts/ci_main_en_verde.py` (solo stdlib + `gh`). **Tests:** `tests/test_ci_main_en_verde.py` (veredictos, corridas sin veredicto, eventos que no son de main, etiqueta, re-evaluación, respuestas inesperadas; y anclas contra el YAML real: disparadores, nombre del check, permisos por job, concurrencia, pinning, `env`). **Pendiente del propietario:** agregar «🚦 Main en verde» a los checks obligatorios de `main`; hasta entonces el check informa pero no bloquea. **No verificado:** que el `GITHUB_TOKEN` pueda re-correr el job (solo corre desde `main`, después del merge).
- **CI: corrida nocturna, ejecución a pedido y alerta de `main` roto.** `ci.yml` corre la suite completa cada noche (07:00 UTC) y con `workflow_dispatch` sobre cualquier rama. Si sobre `main`, fuera de un PR, falla cualquier job no informativo, el job `alerta-main` abre el issue «CI de main roto» o comenta el que está abierto (`scripts/ci_alerta_main.py`, solo stdlib + `gh`). Es el único job con permiso de escritura (`issues: write`), y los datos entran por `env`. **Tests:** `tests/test_ci_alerta_main.py` (crear/comentar/título exacto/respuesta inesperada/error de uso, con un `gh` falso) y `tests/test_ci_platform_policy.py` (disparadores, la alerta cuelga de todo job no informativo, mínimo privilegio enumerado). **No verificado:** que la alerta abra el issue en GitHub de verdad (solo se dispara con `main` en rojo).
- **El timeout de un job VFS dice en qué fase se quedó (R0-1).** En el rig un job sin resultado se reportaba siempre igual (`excedió Ns`), sin distinguir un bridge que no confirmó el lanzamiento, un worker que no llegó a conectarse (arranque lento del ejecutable) y un worker ya trabajando. Con paradas de 1 a 2,5 minutos de la máquina entera, esa atribución hacía falta para no leer el entorno como un defecto de contención.
  - El broker guarda por job los hitos `launch_ack`, `worker_hello`, `job_result` y `worker_exit` (reloj monótono; solo sirven para atribuir, no deciden nada) y `VfsJobTimeoutError` trae `fase` (`lanzamiento`, `arranque` o `ejecucion`) y `hitos`. El mensaje conserva el prefijo `job <id> excedió <N>s` y suma la fase; además sale un `warning` estructurado (`vfs_job_timeout`).
  - **Las dos superficies que esperan el resultado** (`submit` y la sesión) construyen el error por el mismo método (`_error_de_timeout`), y las dos funciones de limpieza del registro borran los hitos. Ambas cosas se anclan por AST en `tests/test_vfs_broker.py` (un sitio nuevo que construya el timeout a mano, o una limpieza que olvide los hitos, rompe el test).
  - **No cambia ningún plazo ni la política de cuarentena.** No toca el bridge: no hay que reinstalarlo.
- **La GUI muestra el progreso de la etapa 9 y la cuarentena del broker VFS (P-3c-2, H-11 y H-4).** Hasta ahora solo los veía el agente (`lods_status`): el operador frente a la GUI no podía distinguir «generando» de «colgado» ni saber que su instancia estaba aislada.
  - Un panel en los rituales se refresca con el timer vivo del dashboard (lectura en memoria): con una corrida en curso muestra la herramienta, el tiempo, el tamaño del log y desde cuándo no cambia, y la edad de la muestra; con la instancia en cuarentena muestra el motivo y el procedimiento (`vfs_quarantine.como_liberar`), **también sin corrida**.
  - **Una sola fuente y un solo formateador:** `dyndolod_progreso.progreso_en_hechos` arma los hechos que cuentan `lods_status` y la GUI (antes vivían dentro de `lod_tools`); la cuarentena sale del mismo método de contexto, ahora público (`AppContext.estado_de_cuarentena_vfs`, antes `_estado_de_cuarentena_vfs`).
  - **Son hechos, no un veredicto:** ningún texto dice «colgado»; lo juzga el operador con la edad de la muestra a la vista. Es solo lectura: liberar la cuarentena sigue siendo `--mode vfs-quarantine` con Sky-Claw cerrado.
  - Best-effort: una falla al leer el progreso o la cuarentena se loguea y se omite; nunca rompe el dashboard. El progreso lo decide el ciclo de vida del servicio (la muestra se descarta al iniciar y al terminar cada corrida), así que se ve también lo que lanza el agente por Telegram o `/api/chat`, que no pasa por el single-flight de la GUI.
  - **Tests:** `tests/test_gui_estado_de_la_etapa9.py` (paridad GUI/agente por igualdad de hechos, anclas por AST de quién arma las claves, el timer y el callback); el ancla de consumidores de `progreso_actual` suma la lectura de la GUI a propósito. **No verificado:** el aspecto del panel en un navegador real (`sky_claw.app.gui.*` está exento de mypy).
- **La GUI elige el modo de interacción de la etapa 9, y «Continuar DynDOLOD» hereda el de la corrida (P-3c, H-12).** Antes la GUI despachaba `generate_lods` con payload vacío, o sea SIEMPRE asistido, sin forma de pedir el desatendido que el agente remoto ya podía pedir.
  - La tarjeta de DynDOLOD tiene un selector «Asistido / Desatendido» (por conexión, como «Modo local»; default asistido). `on_ritual_run` lo lee en el click, el único punto con contexto de cliente, y despacha `{"interaction_mode": <modo>}` explícito (`ritual_runner.payload_de_generar`); los demás rituales conservan el payload vacío.
  - Solo se acepta el `str` exacto del contrato (`ModoDeInteraccion`): un alias, otra capitalización, un bool o un valor corrupto en el almacén caen al asistido, nunca a «desatendido».
  - **Continuar hereda el modo:** `resume_action_from_result` copia `interaction_mode` del resultado que produjo el handoff (el servicio lo fija en todos sus resultados), para que una corrida desatendida no siga como asistida. Sin un modo válido en el resultado conserva el payload histórico.
  - **El desatendido NO pregunta en esa ventana:** sus aprobaciones (configuración, modal de arranque, etapas previas) viajan por el canal remoto del guard (`CATEGORIA_DYNDOLOD_DESATENDIDO`, no auto-aprobable); sin canal remoto la corrida se corta y no cae a asistido. El texto de ayuda del selector lo dice.
  - **Tests:** `tests/test_gui_modo_de_interaccion.py` (anclas por AST: los rituales con modo son exactamente los cuya strategy acepta `interaction_mode`; el click handler pasa el payload; el modo se lee solo en el click handler). **No verificado:** el aspecto del selector en un navegador real (la GUI es `sky_claw.app.gui.*`, exenta de mypy; solo se probó la lógica y el contrato de tema).
- **Backend brokered de TexGen/DynDOLOD seleccionable en producción (E3-R3).** `build_brokered_dyndolod_spawn_strategy` existía pero nadie lo llamaba: la composición aceptaba `dyndolod_spawn_strategy` y `SupervisorAgent` no lo pasaba, así que la etapa 9 corría siempre standalone. Ahora `dyndolod_backend` (`config.toml`, default `standalone`) decide el backend y `resolve_dyndolod_spawn_strategy` arma la strategy en `AppContext` con las raíces del arranque (datos de la instancia MO2, mods, instalación, `<juego>/Data` y los `-o:` por herramienta derivados del workspace). Pedir `brokered` sin poder cumplirlo, con una instancia que no corresponde o con un valor desconocido devuelve un guard que falla cerrado en el spawn y en el gate de handoff: nunca cae a standalone. **`WIRING_SECURITY_HOLD` aplicado en código (`WIRING_RUNTIME_HOLD_ENFORCED`):** la contención del worker en el Job Object (fence de supervisión #856, identidad del Job #860/#872, adopción de descendientes #877) está implementada pero no verificada de punta a punta, así que, mientras la constante interna `_CONTENCION_DEL_WORKER_VERIFICADA` (no configurable) sea `False`, pedir `brokered` devuelve el guard del HOLD ANTES de construir la strategy —aun con broker y rutas válidos—, sin sesión en el broker, sin subprocess local y sin fallback a standalone. No pasa a `True` por fusionar un PR de contención: exige evidencia sobre el binario final en un sandbox Windows y el visto bueno del auditor independiente, en un PR aparte; ver `docs/audits/2026-11-01_e3r3_job_object_supervision_fence.md`.
- **Auditoría nativa de plugins con el xEdit Bridge ([ADR 0018](docs/adr/0018-auditoria-nativa-xedit-bridge.md)).**
  - Dos herramientas, en la GUI (`tool_dispatcher`) y en el agente LLM (`AsyncToolRegistry`), sobre el mismo `XEditBridgeService`:
    - `auditar_plugin_nativo`: chequeo `-CheckForErrors` de SSEEdit sobre la copia admitida. Es de lectura y no pide aprobación.
    - `admitir_plugin_nativo`: copia el plugin y sus masters, resueltos con la precedencia de MO2, al laboratorio del bridge. Pide aprobación humana (gate HITL en la GUI, `HITLGuard` explícito en el agente) y no modifica MO2. El pedido muestra el nombre entero y con los caracteres invisibles escapados (`nombre_visible`).
  - **Agente LLM y el plazo del router (120 s, que cancela la tool):** la auditoría vence antes por su cuenta (motor ≤ 80 s más 30 s de margen). La admisión corre como trabajo en segundo plano, como `generate_lods`: responde enseguida, y el pedido de aprobación y el aviso de fin viajan por el canal del guard. El `AppContext` registra su cierre. El resultado de la auditoría se poda para que entre en el recorte del router (`MAX_TOOL_RESULT_CHARS`) como JSON válido.
  - **Cliente:** el bridge se lanza como proceso aparte, por MCP stdio, una sesión por llamada, con timeout y Job kill-on-close.
    - El arranque (comando, args, cwd y env) se lee completo del `codex-mcp.example.toml` del bridge: los args tienen que ser exactamente los que se ejecutan y el cwd, la propia raíz. La instalación se configura con una sola variable, `XEDIT_BRIDGE_ROOT`, validada contra el sandbox.
    - Versión fijada a `>=1.1.0,<2`. Todo error falla cerrado, también una respuesta malformada o un `ok` que no es booleano.
    - Un id JSON-RPC repetido o una salida del protocolo que no es UTF-8 válido también fallan cerrado.
    - La ruta del bridge tiene que caer dentro del sandbox, que define `install_dir` de `config.toml` (gana sobre `--install-dir`). Documentado en la guía de configuración.
  - **Lo que se aprueba es el contexto exacto:** el pedido muestra el perfil y la instancia MO2 de los que se copia, y la admisión los vuelve a comprobar tras la aprobación: si cambiaron durante la espera, no se copia nada. Un veredicto del bridge incoherente (`clean=true` con errores, hallazgos o fallas) no aprueba.
  - **Veredicto:** `success` indica que la auditoría es confiable (completa, integridad `PASS`, sin recortar), y solo `aprobado` habilita tratar el plugin como sin errores. Los textos del plugin se sanean y se acotan.
  - **MO2:** `PathResolutionService.get_mo2_instance_ini_dir()` entrega la carpeta del `ModOrganizer.ini` del que Sky-Claw derivó la instancia (global o portable), para que el bridge lea ese mismo INI y no la instalación ni una copia vieja en la raíz de datos.
  - **`run_capture`:** acepta `input_bytes` (stdin) y `env`; sin ellos el comportamiento no cambia.
  - **Tests:** el ancla `test_xedit_bridge_superficies` exige cada herramienta en las dos superficies. El smoke contra el bridge instalado es opt-in (`SKYCLAW_SMOKE_XEDIT_BRIDGE=1`).
- **Los workflows que construyen `SkyClawApp.exe` registran su SHA-256 (`scripts/registrar_sha256_ejecutable.py`).** La evidencia sobre el binario final solo vale si se sabe CUÁL fue: `ci.yml`, `release.yml` y el laboratorio de Windows (`vfs-supervision-sandbox.yml`) calculan el hash justo después de construir y lo dejan en el log y en el resumen del job; el laboratorio además guarda un JSON con el contexto del run (commit, job, run id) en su artefacto de resultados y, tras el último uso del ejecutable, **verifica** que sea el mismo archivo que se hasheó (falla ante cualquier duda). Anclado por igualdad sobre la familia de productores en `tests/test_release_smoke_wiring.py`: un productor nuevo sin registro rompe el test. Un hash identifica ESTE archivo; no se verificó que dos builds del mismo commit produzcan los mismos bytes. El job de `release.yml` está pausado (`if: false`): su paso nuevo no corre hasta reactivarlo y por ahora solo lo cubre el ancla. `build.bat` (build local de desarrollo) queda fuera.
- **Cuarentena del broker VFS operable sin borrar dotfiles (`--mode vfs-quarantine`, H-4 de la auditoría de la etapa 9).** Cuando el broker pierde la certeza de que un job terminó deja la instancia en cuarentena y la persiste (marcador `.<instancia>.quarantine`, o un lock «envenenado» si no pudo escribirlo); `release_quarantine` solo sirve con un broker vivo que ya es dueño del lock, y con el lock envenenado ni siquiera se puede arrancar uno: el operador salía borrando archivos a mano. `sky_claw/local/mo2/vfs_quarantine.py` lee el estado (solo lectura) y lo libera con evidencia explícita y solo si NO hay un dueño vivo del lock; cubre las tres formas persistidas, porque borrar solo el marcador no alcanza (el próximo arranque reclama el lock huérfano y reimpone la cuarentena). Para decidir si un lock está huérfano reusa la función del broker (`lock_de_instancia_tiene_dueno_vivo`, extraída sin cambiar su comportamiento): una sola definición. Borra el lock —releído y sin cambios— antes que el marcador, y deja `CRITICAL` con la evidencia. `lods_status` del agente suma `cuarentena_vfs` (razón y cómo liberarla) cuando hay cuarentena. Los tests fabrican cada estado con el broker REAL y exigen que lo que dice el módulo coincida con lo que el broker hace al arrancar. Un lock envenenado ahora guarda el `create_time` de su dueño en LOS DOS sitios que lo escriben (`proceso_dueno_sigue_vivo`, también extraída del broker): un PID que el SO reutilizó para otro proceso deja de bloquear la liberación; un lock envenenado antiguo, sin `create_time`, sigue conservador. El comando para liberar lleva su ejecutable (`SkyClawApp.exe` si el runtime es el congelado, `python -m sky_claw` si no; antes salía «corré --mode …», imposible de correr tal cual) y el `--mo2-root` de la sesión (CLI y `lods_status`): con otra raíz `release` no encontraba nada y callaba; ahora avisa de la instancia que sí está en cuarentena. Un `timestamp` irrepresentable en el marcador ya no aborta la lectura ni la liberación. Un rechazo de `release` (sin evidencia, dueño vivo, disco) sale con código 1 y el motivo en el log, no con un traceback bajo «Fallo no manejado». Mutaciones verificadas: no borrar el lock, no borrar el marcador, permitir liberar con dueño vivo, no exigir evidencia, ignorar el lock huérfano, saltear la relectura, ignorar el `create_time`, quitarlo de un escritor, no atrapar el desbordamiento, volver a la constante sin raíz, quitar el lanzador del comando y sumar un escritor de lock hacen fallar al menos un test. **No incluye:** verla en la GUI (P-3), liberarla desde un broker vivo, una corrida en Windows real, ni cerrar la ventana mínima entre la relectura y el `unlink` del lock (es la del propio reclamo del broker; el procedimiento pide cerrar Sky-Claw antes).
- **Política de teardown tras el resultado: una cola lenta del proceso lanzado ya no deja la instancia en cuarentena (`VfsExecutionBroker`, `post_result_cancel_grace_seconds`).** Con el `job_result` ya recibido, si el `worker_exit` no llega en 5 s (por defecto; positivo y finito) el broker le pide al bridge cerrar el Job con el `cancel` de siempre, una vez, y espera hasta el MISMO deadline duro de 30 s (pedirlo no lo extiende, ni siquiera si el envío se traba: queda acotado por ese deadline). Un MO2 real midió 0,23–0,75 s en lo normal y colas de ~22 s y ~43 s por la salida lenta del bootloader: 1 de 24 corridas terminó en cuarentena con el resultado ya recibido (medición de otra sesión, no reproducida aquí). No relaja el fail-closed: `worker_exit` solo se emite con el Job vacío; si el bridge no puede cerrarlo o no lo confirma antes del deadline, la cuarentena sigue igual, y un `job_unknown` es una carrera benigna. Cubre `submit()` y las sesiones (comparten `_await_job_completion`; la lista de quién espera `worker_exit` está congelada por AST en `tests/test_vfs_teardown_deadline.py`). No cambia `_CONTENCION_DEL_WORKER_VERIFICADA` ni el protocolo.
- **La etapa 9 asistida desde la GUI verifica las etapas previas y pide tu confirmación si faltan (D-3, H-12).** Hasta ahora solo el modo desatendido y el agente de Telegram comprobaban que Wrye Bash, Synthesis y el grass cache estuvieran hechos antes de lanzar DynDOLOD; la corrida ASISTIDA de la GUI no, «por decisión del owner» (SOP §5.2). El owner revirtió esa decisión el 2026-11-11: ahora `GenerateLodsStrategy` también verifica en asistido y, si faltan etapas, le pregunta al operador por un modal de la GUI; sin su confirmación, sin canal o con un canal que falla, no arranca (fail-closed). El modal necesita su PROPIA categoría HITL (`dyndolod_etapas_previas_asistido`): `make_gui_hitl_notify` solo estaciona las categorías que conoce y manda el resto al canal remoto (Telegram), así que reutilizar la del override remoto habría hecho la pregunta a quien no está frente a la GUI. La nueva se estaciona con su pestaña dueña y **nunca** se auto-aprueba por «Modo local». El aprobador (`AprobadorDesatendidoHITL.aprobar_sin_etapas_previas_asistido`) llega a la strategy por `OrchestrationDispatcherDependencies.aprobar_sin_etapas_previas`, armado en la composición sobre el mismo `HITLGuard`. Consecuencia que conviene saber: el «Continuar DynDOLOD» (`run_texgen=False`) pasa por el mismo dispatcher, así que vuelve a verificar y, si siguen faltando etapas, vuelve a preguntar. Un `interaction_mode` inválido no se frena acá: lo rechaza el servicio. Mutaciones verificadas: 11 supresiones hacen fallar al menos un test (más un mutante equivalente, explicado en el PR). **No incluye:** el selector de modo en la tarjeta de la GUI ni el supervisor en `--mode telegram` (T-1), que van aparte.
- **Herramienta para medir un build nuevo de TexGen/DynDOLOD y darlo de alta en el contrato de UI (`scripts/dyndolod_medir_contrato_uia.py`, H-8).** El modo desatendido solo opera builds medidos en un rig real (hoy Alpha-209 SSE); cualquier actualización o un rig VR cae en `UNATTENDED_BUILD_UNSUPPORTED` y no había con qué medirlo ni un procedimiento. `medir` observa la UI de un proceso vivo por el puerto de LECTURA `ObservadorUIA` y guarda hechos (ventanas y controles con tipo, clase, nombre y `AutomationId`, más el SHA-256 calculado del archivo); `diagnosticar` clasifica CADA captura como lo hace el driver de producción (`clasificar_pantalla`: diálogos primero, con la marca `#32770` de nivel superior o anidada; instrucción = exactamente un `MainInstruction`; botones de la clase contractual; acciones que resuelven a UN elemento) y agrega las clasificaciones contra un contrato congelado: pieza por pieza, `coincide`/`difiere`/`no_observado`. Se toma una captura por momento y con a lo sumo un diálogo; un cambio de título entre capturas no cuenta como dos ventanas. La lógica está duplicada (el script no puede importar al driver, que es la superficie que actúa) y un test de PARIDAD enumerado la compara con el driver real. No mide si un control está habilitado. **«Compatible» exige haber observado todo**: sin las instantáneas del diálogo terminal y de los modales el veredicto es `incompleto`, nunca un «sí» por omisión. La identidad del proceso es estricta (ruta exacta, una sola instancia, nunca por nombre) y no se amplía el alcance a otros pids; una enumeración incompleta o la ausencia de UIA cortan sin dejar una medición parcial; el informe no se escribe dentro del directorio de la herramienta. Es de solo lectura por contrato: un ancla por AST enumera los scripts que importan la superficie UIA y les prohíbe nombrar primitivas que actúen o importar al driver. Procedimiento en `docs/operations/dyndolod_alta_de_build_uia.md`. Mutaciones verificadas: 11 supresiones hacen fallar al menos un test (una sobrevivió a la primera y reveló un test que pasaba por otra razón; está corregido) y, tras la revisión de Codex, 8 más sobre la clasificación por captura (botón de cualquier clase, instrucción sin exigir una sola, marcas anidadas, acciones duplicadas, dos diálogos, dos ventanas principales, veredicto sin «incompleto», build mezclado). La primera versión del diagnóstico era más laxa que el driver (mezclaba ventanas de momentos distintos, ignoraba los diálogos anidados y no exigía la clase del botón ni una sola instrucción): un build podía salir `compatible` sin que el driver pudiera operar su diálogo. Tras la revisión de CodeRabbit: una captura de un diálogo que ningún contrato reconoce ya impide el `compatible` aunque estén todos los conocidos; `diagnosticar` calcula el `sha256` de `--exe` y se niega si no es el ejecutable medido (el preset de un build no avala a otro, ni tras una actualización en la misma ruta); un informe sin `exe_sha256` válido se rechaza al cargarlo; `medir` falla antes de medir si la carpeta de salida no existe (no se pierde una medición al guardarla); y el ancla por AST ve todas las formas de importar el driver y la superficie UIA (`import a.b.c`, `from a.b import c`), también en los módulos de solo lectura del paquete. **No incluye:** el alta de ningún build (es un PR con evidencia del rig) ni una corrida sobre la GUI real: hoy solo se probó con una GUI simulada derivada del propio contrato congelado.
- **La etapa 9 informa su progreso: «generando» ya se distingue de «colgado» (H-11, `pipeline.dyndolod.progress` y `lods_status`).** TexGen/DynDOLOD tardan horas y el latido del runner solo escribía una línea al logger: GUI y Telegram no podían distinguir una corrida viva de una trabada. Ahora `DynDOLODRunner` entrega, en cada latido, una muestra de HECHOS por un callback (`on_progress`): la herramienta, los segundos transcurridos y, si existe, el tamaño del log y los segundos desde su última modificación (`ProgresoDeHerramienta`; se lee con `stat` sobre la ruta de `_ruta_del_log`, sin abrir el archivo). El servicio la guarda como `progreso_actual` —de UNA corrida: se descarta al iniciar la siguiente y al terminar— y la publica como `pipeline.dyndolod.progress` (`DynDOLODPipelineProgressPayload`); el snapshot se guarda antes de publicar, así que no depende de que el bus esté vivo. `lods_status` del agente la expone como `progreso`, solo mientras la corrida del trabajo está `en_curso` (antes de lanzarla, esperando la aprobación, el snapshot del servicio sería el de otra corrida), junto con la edad de la muestra: no hay un veredicto «colgado», el umbral lo juzga quien lee. El observador es best-effort: su falla se registra (`dyndolod_progreso_no_reportado`, exento de etapa con su motivo) y no cambia el resultado de la corrida, y un observador que no vuelve no retrasa su fin ni frena los latidos siguientes (la entrega de cada muestra tiene un plazo de 5 s). El único sitio de producción que construye el runner lo cablea y el conjunto de consumidores del snapshot está congelado por AST (hoy solo `lods_status`). Mutaciones verificadas: 9 supresiones hacen fallar al menos un test (y, tras la revisión de CodeRabbit, sin plazo de entrega, con el estado de trabajo vivo en vez de `en_curso` y sin acotar las mediciones del log). La medición del log tiene a lo sumo UNA en vuelo, en un hilo daemon: un `stat` colgado (disco de red caído) ya no suma un hilo del executor por defecto por latido —al agotarse, todo otro `to_thread` de la app quedaba esperando—, las muestras se omiten mientras no vuelve y se reanudan solas (también si el hilo no llega a arrancar: la bandera se libera); que el hilo sea daemon lo exige además el gate de ciclo de vida de `tests/conftest.py` (un hilo no-daemon vivo al final de la sesión sale con código 3). `execute` descarta el snapshot de la corrida anterior de forma síncrona ANTES del preflight (el gestor marca `en_curso` sin ningún `await` hasta `execute`, y `_publish_started` llega recién después del preflight): sin eso, el rastro de una corrida que no se limpió se veía como progreso del trabajo nuevo; el preview (`dry_run`) no lo toca. Límite declarado: `elapsed_seconds` es monotónico pero `log_sin_cambios_segundos` y la edad de la muestra son de reloj de pared (el `mtime` del log no tiene equivalente monotónico), así que un salto del reloj los corre; el criterio robusto es que `log_bytes` crezca entre muestras. Convive con `cuarentena_vfs` (#904) en la misma respuesta de `lods_status`: tests enumerados por cada estado del trabajo exigen que la cuarentena se informe siempre (también sin trabajos) y el progreso solo `en_curso`, y que la falla de uno no apague al otro. **No incluye:** distinguir la corrida de un trabajo de una corrida directa que se le superponga mientras está `en_curso` (el snapshot no lleva la identidad de la corrida; el lock transaccional de la instancia impide que dos avancen a la vez),  el consumidor de la GUI (P-3b), un aviso periódico por Telegram (hoy se consulta con `lods_status`) ni la medición en un rig de si, bajo USVFS, el log de la herramienta aparece en la ruta que mide el runner (si no, tamaño y antigüedad quedan en `null` y solo vale el tiempo). Corrige además la cifra de exenciones del SOP (`sky_claw/local/AGENTS.md`: decía 15, en `main` eran 18; ahora 19).
- **Auditoría de la etapa 6 asistida en el log (#880, H-5).** `ejecutar_fase_asistida`, el punto único de las tres superficies (GUI, `SupervisorAgent` y tool del agente), registra un `INFO` por fase con `success`, `reason`/`motivo`, `frescura_probada`, `testigo_origen`, `testigo_contexto`, `testigo_durable` y `estado_en_plugins`. Lista blanca sin rutas ni `message` libre. El sensor de overwrite no cambia (H-4 se documenta en `docs/operations/etapa6_smoke_rig.md`).
- **Fence de supervisión del worker del broker MO2/USVFS.** El bridge hace `startApplication` (el worker nace corriendo) y recién después `AssignProcessToJobObject`; un descendiente creado antes queda fuera del Job y sobrevive a su terminación, y nada ordenaba el despacho del worker después del `launch_ack`: medido, el worker creaba el nieto de la attestation y despachaba la herramienta sin confirmación. Ahora el worker espera un mensaje `supervised` (autenticado, ligado a su `job_id`) antes de crear el nieto de la attestation y de despachar la herramienta (lo que el intérprete crea al arrancar queda fuera del fence); el broker lo envía una sola vez, solo tras el `launch_ack` del bridge para ese job y mientras el job no esté en cancelación. Cubre LOOT, `vfs-health`, TexGen y DynDOLOD por igual. El bridge instalado no cambia; **actualización coordinada**: daemon y worker son el mismo ejecutable (un worker sin la capacidad es rechazado). Límites y propuesta para la contención del proceso Python bajo PyInstaller onefile en `docs/audits/2026-11-02_fence_de_supervision_del_broker.md`.
- **Identidad del Job del worker PyInstaller onefile (P2, apilado sobre el fence de supervisión).** El fence prueba «el bridge asignó el proceso que lanzó MO2» (en onefile, el bootloader), no «el proceso que ejecuta Python está en el Job»: un hijo Python nacido antes del `assign` queda fuera del Job. Ahora el broker elige por launch un nombre de Job inadivinable (`Local\SkyClawVfs-<128 bits>`, en `_registrar_job`, común a `submit` y `open_session`); el bridge crea el Job con ese nombre antes de lanzar (rechaza uno preexistente, DACL de solo consulta) y lo confirma en el `launch_ack`; el worker efectivo lo abre con `JOB_OBJECT_QUERY`, comprueba `IsProcessInJob` contra **ese** handle (nunca `NULL`), exige `KILL_ON_JOB_CLOSE` sin breakaway y que sus descendientes vivos estén dentro, cierra su handle y recién entonces habla con el broker; si falla intenta terminar a sus descendientes (no está garantizado: si la limpieza también falla, ese proceso puede quedar vivo) y sale con 72 sin abrir un socket. El broker solo autoriza si el ack **y** el `hello` nombran el Job que pidió. `VFS_PROTOCOL_VERSION` no cambia: un bridge, broker o worker sin la capacidad se rechaza de forma explícita. **Actualización coordinada de bridge y daemon.** `_CONTENCION_DEL_WORKER_VERIFICADA` (#854) sigue en `False`; el análisis, las mediciones del arranque del `.exe` congelado y los límites están en `docs/audits/2026-11-03_identidad_del_job_onefile.md`.
- **El testigo de la fase 1 de la etapa 6 pasa a ser durable entre sesiones (PR D de [ADR 0016](docs/adr/0016-etapa-6-wrye-bash-asistida.md) §7.2).**
  Hasta el PR C el `testigo_previo` vivía lo que vivía el resultado en `ReactiveStore` (RAM) y en la
  superficie del agente LLM ni siquiera podía viajar porque `GenerateBashedPatchParams` sólo expone
  `fase` (A-4): cerrar la GUI entre las dos fases o verificar desde el agente obligaba a declarar
  `frescura_probada=False`. `sky_claw/local/tools/etapa6_testigos.py` persiste la observación previa
  (ausencia probada `existia=False, sha256=None` o presencia con digest SHA-256 cuando el artefacto
  era regular y legible; sin entrada cuando la observación es indeterminada) en
  `SystemPaths.runtime_state_dir() / "etapa6_testigos.json"` (exclusión por ruta canónica
  intraproceso + lockfile cross-process `flock`/`msvcrt.locking`, escritura atómica `mkstemp` +
  `os.replace`, rechazo de enlaces con `link_kind_or_raise` y de archivos especiales/directorios con
  `S_ISREG` + `O_NONBLOCK` + `fstat(fd)` en archivo y lockfile, `AlmacenDeTestigosIlegibleError`
  para no pisar un archivo corrupto y warnings estructurados con `pipeline_stage=6` + `tx_id=None`),
  claveada por el **slot virtual** `(plugins_txt canónico, destino Data canónico)` — invariante
  cuando la primera corrida pasa de `Data` ausente en la fase 1 a `overwrite` en la fase 2.
  `decidir_testigo_de_preparacion` separa en el dominio los cuatro casos de reapertura y el servicio
  expone una partición total en `testigo_durable` (`guardado`, `conservado_por_build`,
  `conservado_sin_observacion`, `sin_observacion`, `no_disponible`, `error_de_almacen`): si el
  operador reabre la GUI en frío después de construir y vuelve a pulsar la etapa 6, conserva el
  testigo previo al build y lo reemite en `testigo_previo` (marcando
  `construido_despues_de_la_preparacion=True`) para que el nuevo CTA demuestre frescura en vez de
  fallar con `SIN_CAMBIO`. Toda sincronización de fase 1 ocurre dentro de una única transacción
  atómica del almacén (`sincronizar_preparacion`: `cargar → observar → decidir → escribir`, sin
  locks globales del pipeline) y etiqueta cada época con un `generacion` de 128 bits propagado en
  `testigo_previo` mediante un único parser cerrado (`parsear_testigo_de_payload`, compartido por
  GUI y servicio). En la fase 2 rige `payload` > `durable` > `ninguno` (`testigo_origen`), sin
  sustituir un payload explícito malformado por estado de disco; una verificación fallida (p. ej.
  `NO_HABILITADO`) preserva el testigo para el reintento y una con `frescura_probada=True` ejecuta
  consumo condicional CAS (`olvidar_si_coincide` exigiendo `generacion` explícita y válida) sólo
  cuando el testigo verificado trae su época `generacion`, de modo que ni un replay obsoleto ni un
  payload legítimo sin `generacion` (`no_disponible` / `error_de_almacen`) pueden borrar el testigo
  durable de una preparación posterior. Cableado en `orchestration_composition` y en
  `system_tools.generate_bashed_patch` (el agente LLM gana frescura demostrada entre sesiones sin
  exponer `testigo_previo` al modelo).
  Tests: `tests/test_etapa6_testigo_durable.py` (53 funciones / 79 casos + anclas AST y de firma
  para construcción, mutación atómica/condicional sin comodín `None` y delegación de parser,
  incorporada también a `test-posix-core` en `.github/workflows/ci.yml` y anclada en
  `tests/test_ci_platform_policy.py` para ejecutar en Linux las defensas POSIX exclusivas de FIFO
  `O_NONBLOCK` + `fstat(fd)`, `fsync` de directorio padre y `fcntl.flock`; matriz de mutación
  verificada).

- **El panel del ritual ofrece la fase 2 de la etapa 6 (`Ya construí el parche — verificar`).**
  Antes, el botón del ritual sólo podía *preparar*: despachaba `payload` vacío y la
  verificación quedaba alcanzable únicamente por el agente o la API, así que la etapa 6 no se
  completaba desde la GUI. La acción de resume ahora se **deriva del resultado estructural**
  (`ritual_runner.resume_action_from_result`) con una tabla cerrada por igualdad
  (`_ACCIONES_DE_RESUME`: `dyndolod` → handoff, `wrye_bash` → verificar), y el payload deja de
  ser un literal: `run_ritual_resume` recibe `payload` **obligatorio y keyword-only** en vez del
  `{"run_texgen": False}` hardcodeado, que era lo que ataba el resume a DynDOLOD y habría hecho
  que cualquier etapa nueva heredara su intención.
  La fase 2 se ofrece sólo si la fase 1 **se completó** (`pendiente=True`): tras
  `PreflightBlocked`, `SinRutasResolubles` o un veredicto no hay botón — un CTA que sólo puede
  fallar. El `testigo_previo` viaja en el payload cuando es la forma canónica
  (`{existia: bool, sha256}`, con `existia ⇒ sha256`), validada en el seam **antes** de ofrecer el
  botón, de modo que el testigo de frescura sobrevive el round-trip GUI → estrategia → servicio.
  El detalle del CTA deja de rotularse «TexGen Output listo en:» en todas las tools.
  Tests: `tests/test_resume_etapa6_asistida.py` (19 casos, con las mutaciones verificadas); los anclajes
  de `test_resume_hardening.py` pasan a recorrer la cadena completa resultado → payload →
  dispatcher en vez de asumir el hardcode.

- **Etapa 6 (Wrye Bash) pasa a ser una etapa ASISTIDA de dos fases ([ADR 0016](docs/adr/0016-etapa-6-wrye-bash-asistida.md)).**
  Wrye Bash no expone build headless del Bashed Patch —`-b/--backup` es el backup de
  settings y ningún flag reconstruye el parche—, así que la etapa fallaba cerrado
  *después* de tomar los locks `bashed-patch` + `load-order`, emitir el `ActionManifest`
  y abrir una TX: cada invocación dejaba caja negra por un ritual que no puede correr.
  `sky_claw/local/tools/wrye_bash_asistida.py` define el contrato nuevo: **preparar**
  (instrucciones para el operador + huella previa + estado en `plugins.txt`, sin locks y
  sin TX porque no muta) y **verificar** (veredicto por evidencia en disco: artefacto
  existente, regular —ni symlink ni junction—, no vacío y habilitado en el perfil). La
  fase 1 emite un `TestigoPrevio(existia, sha256)`: distingue "lo miré y no estaba" de
  "no traje testigo", así que la primera corrida —crear el parche desde cero— sí puede
  demostrar frescura. El `plugins.txt` se decodifica `utf-8-sig`, igual que el consumidor
  de la etapa 9, porque con BOM un lector de bytes reporta `AUSENTE` sobre un perfil
  válido mientras el guard de DynDOLOD lo ve habilitado.
  El veredicto **no se deriva de ningún exit code** —no hay subproceso que consultar—,
  falla cerrado ante `plugins.txt` ilegible y solo prueba frescura cuando el llamador
  trae la `huella_previa` (nunca por `mtime`). El módulo no spawnea: se mantiene fuera
  del censo `LANZADORES_ESPERADOS` y no reabre la ambigüedad headless. Los dos métodos
  nuevos del servicio (`preparar_etapa_asistida`, `verificar_etapa_asistida`) llegaron
  **sin cablear** a propósito —para no arreglar una superficie y dejar a las hermanas—;
  el cableado de las tres es la entrada del PR B más abajo en esta misma sección.
- **La etapa 6 asistida distingue a sus DOS ejecutores: el runner y el operador.** La
  corrida spawneada lee el `Data` físico; el operador abre Wrye Bash *desde MO2* y lee la
  superposición del perfil bajo la USVFS. `tools/output_targets.bashed_patch_en_overlay`
  resuelve el artefacto por prioridad de capas (`overwrite` → mods → `Data`) para la
  verificación, y `WryeBashPipelineService._ensure_preflight(dominio=…)` construye el
  preflight del dominio correspondiente: bajo USVFS se omite el sensor de visibilidad
  (U-01), que mide el `Data` físico y daba rojo sobre un perfil cuyos mods viven en
  `mods/`, y el sensor de permisos sondea el `overwrite` del perfil en vez del `Data`
  —la VFS redirige la escritura, no hereda sus permisos—. Ese sensor es un **write-probe**
  real: crea y borra `.skyclaw_probe_<uuid>.tmp` en el destino. La fase 1 no muta el
  artefacto, el perfil ni el load order, y las docstrings lo dicen así —«no muta» no es «no
  escribe un solo byte»—; si el borrado falla, el residuo se **reporta** en el preflight
  como `probe_residue` en amarillo (no bloquea) y dos tests de contrato del servicio fijan
  ambas mitades. `TestigoPrevio` gana el tercer estado *no sé* (`None`) y rechaza la forma
  imposible `{existia: True, sha256: None}`.
- **La etapa 6 ASISTIDA queda cableada a las TRES superficies de producción (PR B de ADR 0016 §7).**
  El PR #839 dejó el contrato y el servicio, pero las tres superficies seguían llamando a
  `execute_pipeline`: el camino headless que no puede construir el parche y que, por eso,
  tomaba los locks `bashed-patch` + `load-order`, emitía el `ActionManifest` y abría una TX
  para fallar cerrado **siempre**. Ahora `dispatcher_dependencies.build_wrye_bash_pipeline`
  (GUI/dispatcher), `SupervisorAgent.execute_wrye_bash_pipeline` y
  `system_tools.generate_bashed_patch` (agente LLM/Telegram) delegan todas en
  `WryeBashPipelineService.ejecutar_fase_asistida`, el **punto único** que elige entre
  `preparar` y `verificar`. La decisión vive en un solo método a propósito: es el defecto
  dominante del repo —9 de 13 follow-ups auditados arreglaron una superficie y dejaron a la
  gemela— y tres copias de la elección garantizan que dos se queden atrás.
  `tests/test_wrye_bash_cableado_asistido.py` la congela con un ancla por AST que **enumera**
  los call sites por archivo + función envolvente: una cuarta superficie, o una que vuelva a
  `execute_pipeline`, rompe el test; también se probó por mutación (cuarta superficie → rojo,
  regreso al camino headless → rojo).
- **`fase` y `testigo_previo` entran al filtro de la estrategia `generate_bashed_patch`.**
  Sin esto la fase 2 era **inalcanzable**: el filtro sólo conocía `profile` y `validate_limit`,
  así que un pedido de verificación se convertía en silencio en una segunda preparación.
- **H-1: la tool del agente LLM resuelve rutas con el perfil de la sesión.**
  `system_tools.generate_bashed_patch` construía `WryeBashPipelineService` **sin**
  `path_resolver` —así que `_plugins_txt_path()` devolvía `None` y la etapa nacía con
  `SinRutasResolubles`— y llamaba con el perfil literal `"agent-tool"`, que no existe en la
  instancia MO2. `AsyncToolRegistry` acepta ahora `path_resolver`, `app_context` le inyecta el
  mismo `PathResolutionService` que usa la GUI, y el perfil lo aporta `mo2_profile` (fijo por
  sesión). La tool expone al LLM **sólo** `fase` (`GenerateBashedPatchParams`, con
  `extra="forbid"`): las dos fases son de sólo lectura, el perfil no se delega al modelo y
  `testigo_previo` no se expone porque un hash que el modelo podría inventar convertiría la
  prueba de frescura en una afirmación sin evidencia. Mandar cualquiera de los dos campos
  —o cualquier otro— devuelve un `ValidationError` explícito en vez de ignorarse, para que el
  modelo no se quede creyendo que eligió el perfil o que trajo evidencia. El handler, además,
  **no adivina** un perfil si no se lo pasan (`PerfilInvalido` en vez de preparar contra
  `"Default"`) y sanea `artefacto`/`plugins_txt` con `sanitize_for_prompt` antes de devolverlas:
  se componen con nombres de carpeta de mods, que son texto de terceros.
- **El toast del ritual dejó de decir «falló» sobre la etapa asistida.** Con el cableado, la
  fase 1 empezó a llegar al panel, y `summarize_ritual_result` la renderizaba como
  «El ritual «wrye_bash» falló: Etapa 6 (Bashed Patch) requiere tu intervención…» en rojo: un
  mensaje que se contradice solo y que reporta un fracaso donde había una instrucción. Ahora
  devuelve `warning` con el mensaje de la etapa. Los dos consumidores del contrato (el toast y
  `IdempotencyMiddleware`, que marca `AWAITING_OPERATOR`) comparten el predicado
  `tool_result.es_etapa_pendiente`, para que no puedan divergir.
- **`AWAITING_OPERATOR`: una etapa asistida ya no se registra como `FAILED`.**
  `ToolStateMachine` sólo conocía `PENDING → RUNNING → COMPLETED/FAILED`, así que
  `preparar` —que devuelve `success=False` a propósito, porque no construyó nada— se veía como
  un ritual fallido. `IdempotencyMiddleware` traduce `pendiente=True` al estado nuevo, terminal
  y sin salidas (la etapa se cierra con **otra** invocación en fase `verificar`), que libera la
  clave de idempotencia como cualquier terminal. Un fallo real —preflight rojo, M-04, rutas
  irresolubles— sigue siendo `FAILED`: no trae `pendiente`. El marcador provisional
  `pendiente_no_ratificado` del PR A **se retiró**: dos campos para una sola decisión vuelven
  a divergir.

### Changed
- **Se levanta el `WIRING_SECURITY_HOLD` del backend brokered de TexGen/DynDOLOD (etapa 9).** `_CONTENCION_DEL_WORKER_VERIFICADA` (`sky_claw/local/mo2/brokered_dyndolod.py`) pasa de `False` a `True`: con `dyndolod_backend = "brokered"` y todo válido y coherente, `resolve_dyndolod_spawn_strategy` construye la strategy real, que reciben por el mismo servicio la GUI y el agente LLM. Pedirlo sin poder cumplirlo sigue siendo un guard que falla cerrado, jamás standalone. **El default sigue siendo `standalone`**: sin la clave en `config.toml` nada cambia.
  - Se cumplieron las cuatro condiciones de «Cuándo se levanta» (`docs/audits/2026-11-01_e3r3_job_object_supervision_fence.md`): evidencia sobre el binario final de `91d234f7` (R0-1 24/24 en rig real y laboratorio con el `.exe` congelado 7/7), el APTO del auditor independiente con evidencia propia, el bundle del bridge ejercido en rig (R0-2 4/4) y la política de teardown del #987. La evidencia de rig queda fuera del repo por decisión del propietario.
  - **El mecanismo se conserva:** volver el literal a `False` reimpone el HOLD. Los tests del guard pasan a correr con la barrera reimpuesta (`monkeypatch`), y los nuevos fijan el valor de producción: `True` literal, la strategy real por el resolver y por `AppContext`, y la misma strategy en el servicio de la GUI y del agente (`tests/test_dyndolod_brokered_hold.py`).
  - Quedan abiertos como decisiones del propietario los 🟡 del auditor: LOOT brokered sin barrera, `worker_executable` fuera del digest del bundle y las vías COM/WMI/padre falso sin declarar como límite.
- **CI: un PR ocupa la mitad de los runners de Windows y el reparto de la suite es por duración medida.** Medido con `gh api` sobre 16 corridas: sin cola la corrida tardaba 18-22 min, y el freno real era el tope de 20 jobs simultáneos de GitHub Free, que comparten todas las corridas del repo (cada corrida lanzaba 14 a la vez; con dos PRs activos ya había cola, y una ráfaga de 5 merges dejó 27 jobs esperando).
  - **En `pull_request` la suite corre solo en py3.11**, el intérprete con el que Build y Release congelan el ejecutable. **py3.12 se sigue certificando en cada push a `main`**: una regresión exclusiva de 3.12 aparece después del merge, no antes. La matriz se elige por evento (`fromJSON`), con la misma expresión en los shards y en su veredicto.
  - **6 shards repartidos por la duración medida de cada archivo** (`scripts/ci_duraciones.json`, 642 archivos, 2616 s) y no por cantidad de tests: por conteo, el shard más lento tardaba 905 s de los 2616. Un archivo sin medir se estima con el promedio por test; una calibración mal formada aborta el shard. Se regenera con `python -m scripts.ci_particion calibrar <carpeta> --total 6`, donde `<carpeta>` tiene los artefactos `test-shard-py3.11-*` de UNA corrida verde (`gh run download <id> -p "test-shard-py3.11-*" -D <carpeta>`); `calibrar` rechaza shards que no terminaron en 0. El verificador no cambia: sigue exigiendo que entre todos los shards se haya corrido exactamente la suite.
  - **Lint corre una sola vez, en Ubuntu** (ruff no depende de la plataforma). Mypy sigue en Windows × py3.11/3.12.
  - Los checks requeridos por la protección de `main` conservan sus nombres. El tope de 90 min por shard no cambia en este PR.
  - **Tests:** `tests/test_ci_particion.py` (pesos, calibración enumerando cada forma mal formada, plugin en subproceso, `calibrar`) y `tests/test_ci_platform_policy.py` (la expresión de versiones congelada y espejada desde su texto, cada disparador real del workflow con sus versiones, el intérprete de entrega censado en todos los workflows que congelan `sky_claw.spec`, lint en Ubuntu y mypy en Windows). **No verificado:** la duración real de los 6 shards en CI (se mide en la primera corrida del PR).

### Removed
- **Se retira la compuerta HITL (`HitlGateMiddleware`) sobre `generate_bashed_patch` — PR E de
  [ADR 0016](docs/adr/0016-etapa-6-wrye-bash-asistida.md) §7.** En el PR B (#853) la herramienta
  se dejó deliberadamente en `DESTRUCTIVE_TOOL_PATTERNS` para separar el cableado de la decisión
  de seguridad. Tras retirar `execute_pipeline` (#862), ni `preparar` ni `verificar` mutan el
  parche, el perfil ni `plugins.txt` (sólo corren el write-probe efímero del sensor de permisos en
  `overwrite` y sincronizan el testigo JSON bajo `SystemPaths.runtime_state_dir()`), mientras que
  la construcción real del `Bashed Patch, 0.esp` ya exige acción explícita del operador dentro de
  Wrye Bash GUI. Mantener el gate hacía que con «Modo local» apagado (`auto_approve=False`) la GUI
  abriera dos modales de aprobación sobre lecturas, que sin canal de notificación o sin
  `HITLGuard` fallara cerrado con `HITLApprovalDenied` / `HITLGateUnavailable`, y dejaba una
  asimetría directa con `AsyncToolRegistry` (donde el agente LLM ya invocaba la tool sin gate).
  `generate_bashed_patch` sale de `DESTRUCTIVE_TOOL_PATTERNS` (quedan las 6 herramientas
  destructivas reales), `OrchestrationToolDispatcher` la registra con
  `[ErrorWrappingMiddleware("WryeBashPipelineExecutionFailed"), DictResultGuardMiddleware("InvalidWryeBashPipelineResult")]`,
  y `GenerateBashedPatchStrategy.describe_for_approval` se retira para no dejar código muerto ni
  satisfacer falsamente `ApprovalPayloadDescriber`. Los 4 censos que enumeran las herramientas
  destructivas (`test_hitl_destructive_gate.py`, `test_dispatcher_dependencies.py`,
  `test_destructive_strategies_error_contract.py` y `tests/bdd/test_mapa_sop_features.py`) se
  actualizan sin relajarlos, y `tests/test_wrye_bash_cableado_asistido.py` suma pruebas E2E del
  dispatcher sin `HITLGuard` y del flujo completo de `run_ritual` + `run_ritual_resume` con
  `auto_approve=False`.
- **Se retira el camino headless de la etapa 6 (`WryeBashPipelineService.execute_pipeline`)
  — ADR 0016 §7.1.** Tras el cableado asistido del PR B no quedaba **ningún** caller de
  producción: `bash.py` no expone build, así que cada invocación tomaba los locks
  (`Bashed Patch, 0.esp` + `load-order`), emitía `ActionManifest`, abría una TX y fallaba
  cerrado. Con él se van 8 helpers (`ensure_runner` incluido), 3 clases module-level, **555
  líneas netas** (el módulo pasa de 1231 a 676; la cifra original, 570/661, quedó vieja tras la ronda 12) y las dependencias `lock_manager`,
  `snapshot_manager`, `journal` y `wrye_bash_runner` de la firma del servicio (8 → 4
  parámetros). El hueco de saneo de `stdout`/`stderr` que el PR B había asignado a este PR
  **se cierra por retiro**: sin subproceso no hay texto de terceros que sanear, y la tool del
  agente ya no admite un runner. El id de lock `Bashed Patch, 0.esp` se retira con el camino
  —sin tomador sería una mentira sobre la etapa 6— y el literal del artefacto se consolida en
  `BASHED_PATCH_NAME` en los 5 módulos donde estaba suelto. Los 36 tests de
  `tests/test_wrye_bash_service.py` se retiran con el código que probaban: **8 de sus contratos
  siguen vivos** (4 se portaron a la suite asistida) y el mapeo test por test está en el ADR.
  Anclas nuevas: `tests/test_wrye_bash_retiro_headless.py` (8 funciones, 12 ítems; **12/12**
  mutaciones muertas sobre el retiro —incluido el registry del agente, que había quedado con
  un parámetro muerto— y **7/7** sobre el nombre del artefacto, contadas por **AST** como
  pares `(nombre, valor)` y no por número de línea: un comentario o un import ya no rompen el
  ancla, y una segunda definición o un literal suelto sí).
  Tres censos (raíces MO2, mecanismos de rollback, productores de evidencia orphan) detectaron
  el retiro por igualdad literal y se actualizaron sin relajarlos.

### Fixed
- **Ya no parpadean ventanas negras de consola al arrancar el bridge de MO2 (ni en otros lanzamientos).** El `.exe` es `console=False`, y en Windows, cuando un proceso así lanza un programa de consola sin `CREATE_NO_WINDOW`, le crea una consola nueva. Al arrancar el bridge, el broker endurece cuatro rutas con `restrict_to_owner`, y cada una lanzaba de dos a cinco procesos (`powershell`, `whoami`, `icacls`) visibles: unas veinte ventanas que el operador lee como un virus. Los 19 lanzamientos sin flag (permisos, LOOT, `wslpath`, VFS, 7-Zip, el ejecutor del agente, el probe del worker y dos scripts de investigación) pasan ahora `creationflags=creationflags_sin_consola()` (`sky_claw/sin_consola.py`; en POSIX devuelve 0, porque `subprocess` rechaza otro valor). No cambia qué se ejecuta ni sus handles. **Test:** `tests/test_subprocesos_sin_consola.py` enumera por AST cada `subprocess.*` y `asyncio.create_subprocess_*` del paquete; un lanzamiento nuevo sin el flag lo rompe. Además prueba en Windows que un hijo lanzado con el flag no recibe consola. **No cubre:** el `cmd.exe /c ver` que lanza la propia biblioteca estándar en `platform.system()` (Python 3.11), ni las consolas que abren por su cuenta herramientas de terceros como LODGen. **No verificado en el rig:** falta un exe nuevo para ver el arranque del bridge sin parpadeo.
- **Etapa 9 brokered: el move-aside ya no deja los mods de salida habilitados sin carpeta (R0 brokered 1).** La primera corrida real de TexGen/DynDOLOD con `dyndolod_backend = "brokered"` (rig G:, 2026-11-22, exe de `6b1d707e`) falló cerrada ANTES de lanzar TexGen: el servicio aparta `TexGen Output` y `DynDOLOD Output` para su rollback O(1), el perfil los sigue habilitando, y la atestación VFS del spawn exige que cada mod habilitado exista («el mod habilitado 'TexGen Output' no existe»). Pasaba igual con las carpetas llenas o vacías y en las dos herramientas (en DynDOLOD, por `DynDOLOD Output` como capa superior). Los tests usaban dobles y nunca juntaban el move-aside real con la atestación real.
  - **Propiedad del mecanismo:** *durante la corrida, todo mod de salida apartado existe como carpeta propia vacía* (`_recrear_mod_de_salida_vacio`, en el mismo paso del move-aside). Una capa vacía aporta lo mismo que una ausente, así que la herramienta ve lo mismo; lo que cambia es que el perfil queda coherente para TODOS sus lectores (atestación, efectividad del handoff, MO2) sin una exención por lector. Solo si hubo move-aside: la primera corrida no cambia.
  - **Hermanos de la carpeta vacía:** el empaquetado la descarta antes de su propio move-aside (`descartar_si_es_carpeta_vacia`, `rmdir` que falla si tiene algo) para no dejar un segundo backup del mismo mod, y la recuperación de arranque trata una carpeta vacía de un mod de salida como corrida interrumpida y restaura el backup solo (`ProductorDeMoveAside.destinos_nunca_vacios`: únicamente los dos mods, porque un mod empaquetado lleva al menos `meta.ini`; los roots crudos externos siguen en «preservar ambos»). Un enlace o junction nunca cuenta como vacío propio.
  - **Tests:** en `tests/test_dyndolod_service.py`, la atestación real dentro de cada spawn sobre un perfil que habilita los dos mods (reproduce el mensaje del rig en rojo), un backup por mod en el empaquetado y la restauración byte a byte ante un fallo; en `tests/test_rollback_reconciler.py`, la restauración con el mod recreado vacío, la igualdad literal de `destinos_nunca_vacios` y los casos de enlace y junction. **No verificado:** la re-corrida en el rig con un exe que incluya el arreglo.
- **`--mode vfs-health`: el plazo por defecto del job sube de 30 s a 120 s (condición 4 del HOLD, decidida por el propietario el 2026-11-22).** El plazo cubre todo el job: lanzar el worker por MO2, el arranque del onefile y el probe. En el rig real el arranque típico es de unos 2,7 s, pero las tres series de R0-1 tuvieron arranques de 32 s a 44 s, en HDD y en SSD por igual. Con 30 s esos casos fallaban por timeout; el rig los vio porque pasaba `--vfs-timeout 60`. Ancla: `tests/test_main.py::test_c4_el_plazo_por_defecto_de_vfs_health_cubre_los_arranques_lentos_medidos`.
- **Teardown con el bridge conectado: el plazo del `worker_exit` sube de 30 s a 90 s (condición 4 del HOLD, decidida por el propietario el 2026-11-21).** El rig real midió colas de 35 s (R0 ronda 7, serie de control, corrida 23) y 43 s (identidad del Job, corrida 8) entre el `job_result` y el Job vacío, con el worker ya terminado y sin residuos. Con 30 s esas colas caían en cuarentena 1 de cada ~24 corridas. 90 s las cubre con más de 2x de margen. Pasado el plazo sigue siendo fail-closed: cuarentena y `VfsTeardownDeadlineError`. La gracia por pérdida del bridge (30 s, espera una reconexión) y el pedido de cierre tras el resultado (5 s) no cambian. Ancla: `tests/test_vfs_broker.py::test_c4_los_plazos_del_teardown_estan_congelados_y_el_broker_de_produccion_usa_el_de_90s`.
- **El `.exe` congelado que falla sale con código 1 en vez de quedar colgado en un diálogo modal (hallazgo de R0 ronda 6, rig real 2026-11-21).** El ejecutable se arma con `console=False`, y ante una excepción que escapa del script el bootloader de PyInstaller abre «Unhandled exception in script», un diálogo MODAL, y espera a que alguien lo cierre. Sin nadie mirando, el proceso queda vivo para siempre. En el rig, `--mode vfs-health` contra un bridge rechazado quedó colgado en 2 de 2 corridas; se reprodujo en local con el exe, y también ante un error **antes** de arrancar el broker, así que ni el broker ni F1 tenían que ver.
  - **Una sola guarda para todos los puntos de entrada** (`_despachar_sin_dialogo`, en `sky_claw/__main__.py`). Convierte cualquier excepción en `SystemExit(1)`, el código que da Python sin congelar. Si el proceso tiene stderr, deja ahí el traceback; es best-effort: sin stderr heredado se omite, y en `main()` igual queda en el log con `exc_info=True`. La garantía cubre lo que corre **dentro** de la guarda. Los imports de `vfs_arranque` y de los helpers UIA van antes; si fallaran, el exe estaría mal empaquetado, y eso lo ataja el laboratorio congelado, porque sus e2e pasan por esos imports. La usan los **cuatro** despachos del ejecutable: el worker VFS y su nieto de sonda, que en el `.exe` salen a nivel de módulo, **antes** de `main()`; los dos helpers UIA; y el despacho del worker dentro de `main()`. El worker era el hermano más delicado: lanzado por MO2 dentro del Job, con el diálogo abierto el Job no se vacía y el fence espera en vano.
  - **En `main()`, solo la GUI conserva el diálogo,** porque tiene un operador delante. Los demás modos (`cli`, `oneshot`, `telegram`, `security`, `install-vfs-bridge`, `vfs-health`, `vfs-quarantine`) salen con `SystemExit(1)`, también ante un `CancelledError` que llegue arriba. Ese caso antes ni se logueaba.
    - **Todo el preámbulo de `main()` va dentro del `try`** (`setup_logging`, `_ensure_std_streams`, `_parse_args`, `_install_sigterm_handler`).
    - **El modo sale del argv mientras no hay `args`** (`_modo_declarado`). Antes, un fallo ahí caía en el default, que en el `.exe` es `gui`, y abría el diálogo aunque el argv pidiera `vfs-health` (PR-Agent en #984).
    - **Un ancla estructural fija lo que corre fuera del `try`.**
  - **Anclas** (`tests/test_main.py`):
    - cada `--mode` está clasificado y congelado por igualdad contra los `choices` reales del parser;
    - un censo AST fija los cuatro despachos y que ningún `raise SystemExit(<llamada>)` salga por fuera de la guarda;
    - los flags del worker se enumeran desde `FLAGS_VFS`.

    Además, `tests/test_vfs_job_identity_e2e_windows.py` mide el **binario**: el laboratorio lo corre con `SKYCLAW_FROZEN_EXE`. Con el exe de `676f4c29` falla por timeout; con el arreglado sale con `rc=1`.
- **Un perfil aislado ya no lee el keyring del usuario ni descubre su máquina real: `SKYCLAW_ISOLATED=1` (hallazgo de R0, rig real 2026-11-16/17).** Un `AppContext` armado con un `HOME` aislado leyó el token de Telegram del operador del keyring global de Windows y ejecutó long-polling (más de 700 llamadas, todas 401, sin envíos), y el zero-config buscó Skyrim en la máquina real, lo persistió en el `config.toml` aislado y lo admitió como raíz del sandbox. El `HOME` no aísla el keyring, el registro ni las bibliotecas de Steam: son del usuario.
  - **Una sola pieza dueña de la propiedad** (`sky_claw/aislamiento.py`): *el aislamiento solo protege si TODOS los accesos participan*, así que no hay un parche por llamada. Las ~25 llamadas al keyring de 7 módulos pasan por el backend global de `keyring`; con la variable activa se instala UN backend que **falla** y queda cubierto cada sitio, incluidos los futuros. Al importar `sky_claw` solo se FIJA `PYTHON_KEYRING_BACKEND`, **sin importar `keyring`** (el worker VFS importa el paquete antes de probar su Job y `import keyring` llama a `platform.system()`, que en Windows lanza `cmd.exe /c ver`: lo encontró la revisión de Codex); `keyring` lo lee en su primer uso (verificado leyendo el código de 24.0.0, el mínimo declarado, y de 25.7.0, el del lock; las versiones intermedias no se revisaron). El backend se instala explícitamente, por si el interruptor se activa después (el arnés del rig lo hace), al construir `Config` y `AppContext`. Un valor desconocido de la variable **enciende** el aislamiento (fail-closed: un typo no puede dejar al proceso tocando la máquina real).
  - **Se instala el backend que FALLA y no el nulo, a propósito:** con el nulo `set_password` «funciona» sin guardar y `Config._load_from_keyring` creería haber migrado el secreto al keyring y lo **borraría del TOML**; el test `test_el_secreto_en_el_toml_no_se_pierde_con_el_backend_que_falla_y_si_con_el_nulo` lo demuestra con la misma config.
  - **El descubrimiento de la máquina real tiene dos implementaciones y las dos respetan el interruptor** (`AutoDetector`, con sus cuatro finders, y `EnvironmentScanner`, que la GUI usa y que además exporta `SKYRIM_PATH`/`MO2_PATH` a `os.environ`). Se apaga el descubrimiento, **no la configuración**: una ruta que el perfil fijó en su `config.toml` sigue valiendo.
  - **Hermano que el censo cazó:** `FrontendBridge._authenticate` leía el keyring sin `try`; con un backend que falla (el estado de un Linux headless) escapaba `NoKeyringError` en vez del `AuthError` documentado. De las 25 llamadas era la única sin captura. Captura `(KeyringError, OSError)` —la jerarquía que `config.py` trata así— y **no** `Exception`: un bug ahí no es «token no configurado» y el bucle de reconexión ya registra lo inesperado con su traza (observación de PR-Agent).
  - **Hermanos que encontró la revisión (Codex), con su test en rojo primero:** (1) `EnvironmentScanner` guardaba Skyrim y MO2 pero **no sus herramientas**: `_build_search_roots` (raíces comunes y Program Files) y `_find_tool` (`PATH`) seguían descubriendo binarios del host, y la sonda de versión los ejecuta; el ancla enumera todo el registro (`iter_external_tool_specs`) y un escaneo completo con solo xEdit fijado. (2) `_migrate_legacy_json` borraba el JSON legado aunque `Config.save()` hubiera dejado el secreto pendiente (con el backend que falla no lo baja al TOML): ahora lo conserva y reintenta en el próximo arranque (riesgo preexistente, también de un Linux sin keyring; el perfil aislado lo vuelve determinista). (3) el hook del paquete de arriba. (4) **Hallazgo de CodeRabbit:** el arranque de `AppContext` (`_scan_tool_paths`, único llamador de `scan_common_paths`) buscaba LOOT y xEdit en Program Files y las raíces de modding del host y **persistía** lo hallado en la config del perfil, que es el defecto original de R0 en otra pieza. Yo lo había excluido como «instalador de herramientas, iniciado por el usuario» por dónde vive la función y no por quién la llama; enumerar los llamadores lo desmintió. Ahora no escanea, y un censo por AST congela que ese sea su único llamador.
  - **Anclas** (`tests/test_aislamiento_perfil.py`, 88 tests, todos con su mutación atrapada): el censo por AST de los módulos que importan `keyring` (8) y de los que leen el registro (3) congelado por igualdad, con la receta de cada uno; que ningún módulo fuera del interruptor instancie un backend ni use otro almacén de secretos; que **toda** llamada al keyring sobreviva a un backend que falla; los finders de `AutoDetector` enumerados por introspección; y el caso real de R0 (el token del keyring del operador no llega a `Config`, con su control sin aislamiento que sí lo lee). El runbook del rig exige activar la variable (regla 7) y un test lo ancla.
  - **Lo que NO cubre:** la lectura de solo lectura de `HKCU\...\CurrentInstance` que hace la identidad de la instancia de MO2 (`mo2_instance.py`) queda exenta, a propósito y registrada en el censo (pregunta abierta: ¿debe aislarse?); y la suite de tests no instala el backend que falla por defecto, así que un test sin parchear sigue pudiendo leer el keyring del usuario donde lo haya (seguimiento).
- **Un plazo del runner de DynDOLOD ya no puede tragarse la cancelación externa en Python 3.11: era la causa del flake de `test_dyndolod_progreso.py` y podía colgar la limpieza de una corrida.** En 3.11 `asyncio.wait_for` envuelve el awaitable en una tarea interna; si la cancelación de quien espera llega desde que el interno queda liberado hasta que el plazo resume, devuelve el resultado y la cancelación se pierde (3.12 lo reimplementó sobre `asyncio.timeout`, que espera en línea y no tiene esa ventana: verificado en la fuente de la stdlib de 3.11, 3.12 y 3.13). En el latido eso dejaba a la tarea viva (`cancelling() == 1`, detenida en su `sleep`) y `_limpiar_helpers_en_salida_normal` esperaba para siempre en `gather(heartbeat)`, que no tiene plazo. **Medido, no supuesto:** bajo carga hubo 48 cuelgues en 480 corridas con el código anterior y 0 en 480 cambiando solo ese sitio (escenario del plazo vencido: 4 de 400 a 0 de 400). El mismo mecanismo podía perder un shutdown externo en el spawn, el readiness y la espera del proceso, justo lo que R3 dice evitar.
  - **Una sola pieza dueña de la propiedad:** `_esperar_con_plazo(aw, timeout)` en `dyndolod_runner.py` (mismo contrato que `wait_for`, sobre `asyncio.timeout`). Los siete `wait_for` del módulo, enumerados por AST, pasan por ahí. **El reemplazo no es neutro en un sitio, y lo encontró la suite completa y no los tests nuevos:** en la gracia del drenaje `wait_for` absorbía, sin que nadie lo hubiera escrito, la segunda cancelación (su waiter interno), y esperar el `gather` en línea se la reenviaba a los drains a mitad de su propio cleanup, rompiendo R3 (`test_r3_salida_normal_cancelacion_durante_drain_grace_espera_helpers_antes_de_close_job`). Ese sitio lleva `asyncio.shield`. El espía de `test_proc_wait_recibe_solo_el_presupuesto_restante` cambió de seam (parchear `asyncio.wait_for` global capturaba plazos de otros módulos).
  - **Anclas:** `tests/test_dyndolod_plazos_y_cancelacion.py` enumera por fases del bucle la invariante «una cancelación aceptada por una tarea viva nunca se pierde» sobre cada sitio con seam (entrega del progreso, confirmación acotada, aviso al operador, gracia del drenaje) y sobre el helper, y un ancla por AST prohíbe `wait_for` en el módulo (con alias, import y referencia sin llamar). Probado por mutación. **No verificado:** spawn, readiness y `proc.wait()` viven dentro de `_execute_process` y no tienen un test de ventana propio (los cubren el ancla y los tests de presupuesto existentes); los 68 `wait_for` de otros módulos de `sky_claw/` **no** se tocaron y quedan para una auditoría aparte.
- **La etapa 9 ya no deja la muestra de progreso de una corrida que terminó (H-11; hallazgo de CodeRabbit en #937).** La muestra que leen `lods_status` y el panel de la GUI se descartaba al iniciar y en `_publish_completed`, que es lo único que alcanzan las salidas «normales». Una corrida que emitía un latido y después fallaba con la cancelación cayendo dentro del handler (`await _cerrar_tx_tras_rollback` antes de `_publicar_evento_terminal`), o que salía por una excepción o un retorno temprano, dejaba la última muestra visible hasta que arrancara otra corrida.
  - **El mecanismo está en el límite de `execute`, no en cada handler:** el decorador `_descarta_el_progreso_al_salir` descarta la muestra en un `finally` (corre también ante `CancelledError`, y la asignación es síncrona: ni una segunda cancelación la interrumpe). Es un decorador y no un `try` dentro de `execute` para no re-indentar ~1500 líneas ni tocar la familia de handlers congelada. Lo que **no** es una corrida (el `dry_run`, de solo lectura, y un `interaction_mode` desconocido, que se rechaza sin tocar nada) no descarta la muestra de otra corrida en curso.
  - **Anclas:** `tests/test_dyndolod_progreso_servicio.py` ejercita el `execute` real por clase de salida (retorno temprano, excepción, cancelación), que `execute` sea el único llamador de `run_full_pipeline` y lleve el decorador, y congela por igualdad literal quién escribe `_progreso_actual`; `tests/test_dyndolod_service.py` reproduce el caso exacto del hallazgo (latido, fallo y cancelación durante el cierre de la TX). **No verificado:** una corrida real larga mirando `lods_status` (rig).
- **«DynDOLOD solo» bajo el backend brokered ya no figura como un camino sin prueba de visibilidad del load order (H-13, B-4): lo estaba cubriendo el job, y ahora está anclado.** La auditoría de la etapa 9 anotó que el camino sin handoff (`run_texgen=False` sin un `TexGen Output` preservado y gateado) spawneaba con fingerprint de perfil, canary y atestación runtime pero sin una prueba de visibilidad del load order completo, porque el sensor físico U-01 no aplica bajo USVFS. Verificado contra el código: `BrokeredDynDOLODSpawnStrategy.spawn` arma el job con `expected_plugins` (el conjunto completo de plugins activos) en las dos ramas y el worker rechaza el dispatch si alguno no es visible en el Data virtual (`tests/test_vfs_worker.py`); el docstring que decía lo contrario había quedado desactualizado y se corrigió.
  - **Anclas** (`tests/test_dyndolod_solo_brokered_visibilidad.py`): las dos rutas de `spawn` (sin y con handoff aprobado) entregan el mismo conjunto de plugins esperados; y por AST, `spawn` construye **un** `VfsJob`, fuera de toda rama y con `expected_plugins` en el payload, así que una rama nueva con su propio job rompe el test hasta que se decide su evidencia. Probado por mutación (sacar la clave rompe las dos rutas; una rama nueva con su propio job rompe el ancla estructural).
  - **Lo que NO se cierra y queda asentado:** ninguna ruta brokered prueba la visibilidad de los archivos que no son plugins (meshes, texturas) de cada mod; es la hipótesis H-2 y se mide en rig (S-2) antes de diseñar un muestreo. Aplica por igual con y sin handoff, así que ya no es un hueco propio de «DynDOLOD solo».
- **La etapa 9 ya no le pide al operador que apruebe una corrida que el backend no puede arrancar (B-3, H-6).** Antes, la aprobación remota de la configuración desatendida —y la confirmación de seguir sin las etapas previas, en la GUI asistida y por Telegram— se pedía ANTES del `spawn`, y recién ahí se descubría que el bridge de MO2 no estaba conectado (o tenía un bundle viejo, #934). Con el backend brokered y la barrera de contención puesta (`_CONTENCION_DEL_WORKER_VERIFICADA = False`) pasaba SIEMPRE: el operador aprobaba una corrida condenada.
  - **La propiedad:** ninguna pregunta al operador anterior al `spawn` se hace sin haber probado, justo antes, que el backend puede servir la corrida. Una capability nueva y obligatoria de la strategy de spawn, `preflight_backend() -> PreflightDeBackend`, la consulta la ÚNICA pieza `preflight_del_backend` (fail-closed: sin la capability, con un veredicto ajeno o con una excepción es «no listo»; `CancelledError` se propaga). Standalone responde listo sin esperar; brokered mira primero la cuarentena de la instancia (estado local, sin esperar: `open_session`/`submit` la rechazan aunque el bridge esté conectado; el motivo trae el procedimiento para liberarla), y después espera con el plazo corto del broker (`wait_until_ready`, 10 s) devolviendo **su** motivo de rechazo tal cual (el del bundle viejo ya trae la instrucción de reinstalar); el guard de la barrera responde «no listo» con su razón.
  - **Cuatro sitios, dos superficies:** la aprobación remota del runner (corta con el código nuevo `BACKEND_NOT_READY` de `CodigoDesatendido`, *después* de los chequeos locales de contrato y presets, así un preset ausente sigue cortando por su causa sin hacer esperar al bridge), el guard de etapas previas de la GUI asistida (`GenerateLodsStrategy`), el del agente remoto (`GestorDeTrabajosDeLods`) y la **compuerta HITL del dispatcher** (`HitlGateMiddleware`, `tool_execution`), que es la primera pregunta que ve el operador de la GUI: `GenerateLodsStrategy` implementa `prepare_for_approval` (la capability que la compuerta ya llama antes de abrir la espera humana) y el error sale por el wrapping existente. Este último lo encontró la revisión de Codex en #950: el primer censo buscaba preguntas por nombre y no lo vio. El `aprobar_modal` del driver UIA queda fuera por estructura: corre sobre un `ctx.pid` ya vivo, o sea con el `spawn` hecho.
  - **Anclas que enumeran:** `tests/test_dyndolod_preflight_del_backend.py` detecta por AST los sitios que tocan el canal del operador y, sobre todo, **todo sitio de `HITLGuard.request_approval`** (la única primitiva por la que un humano recibe una pregunta: cierra la clase en vez de buscar por nombre), y los congela por igualdad literal clasificados en «antes del spawn (con su responsable del preflight)», «después» y «fuera de la etapa 9» (un sitio nuevo rompe el test hasta que se decide su clase), exige que el preflight preceda a la pregunta en cada uno, que toda strategy de spawn (detectada por forma) declare la capability y que solo `preflight_del_backend` la lea. **No verificado:** el comportamiento contra un MO2 real sin el bridge conectado (rig); el default de 10 s de espera es el de `wait_until_ready`, no una medición.
- **El job «Tests / windows-latest / py3.11» del CI ya no se cancela al 98 % de la suite (`timeout-minutes` 70 → 90).** Con 70 min, `py3.11` se canceló tres veces seguidas (#935 dos veces sobre el mismo head, a los 70 min 16 s; #936 una) con la suite casi completa y ningún test fallando; el agregador `Tests (Pytest)` solo ve el `cancelled` y bloquea el merge de un PR sano. La misma suite pasó `py3.11` completa en 56-58 min en otras corridas y `py3.12` tarda 50-62 min: el runner lento de `py3.11` llega a ~72 min (el peor caso de 2026-10-22 era 2322 s), así que 90 min deja el peor caso extrapolado al ~80 %.
  - El piso queda anclado en `tests/test_ci_platform_policy.py` (`test_el_tope_de_la_matriz_windows_cubre_el_peor_caso_medido`): bajarlo en silencio rompe el test.
  - **Es un parche al síntoma:** la suite casi se duplicó desde octubre. Si vuelve a rozar el límite, el paso siguiente es repartirla en shards, no subir el tope otra vez. **No verificado:** la corrida de este mismo PR es la primera que usa el tope nuevo.
- **El broker rechaza un MO2 bridge cuyo bundle no es el de esta versión de Sky-Claw (B-1, H-5).** `PROTOCOL_VERSION` no cambió con #856, #860 ni #877, así que un plugin
  instalado antes de esos fixes seguía conectándose con el bug vivo y sin aviso. Ahora el `hello` del bridge declara el SHA-256 de TODO su bundle (`bundle_digest` en `protocol.py`:
  rutas relativas y contenido de cada archivo, sin `__pycache__`, bytecode ni la `bridge_config.json` por instancia; un enlace dentro del bundle es un error) y el broker lo compara con el de
  su propio producto (`bridge_bundle.digest_del_bundle_del_producto`, la misma fuente que usa el instalador). Un bridge sin digest o con otro se rechaza con la instrucción de reinstalar
  (`--mode install-vfs-bridge`) y reabrir MO2, y ese motivo se agrega al error de `wait_until_ready` en vez de un «no se conectó» sin causa.
  - El plugin calcula el digest **una vez, al cargar**: tras un reinstall con MO2 abierto, recalcularlo en cada `hello` afirmaría el digest del código nuevo sobre el viejo en ejecución.
  - El instalador registra `bundle_digest` en `bridge_config.json` y verifica que la copia en staging coincida con la fuente antes de hacer el swap; si no, aborta sin tocar la instalación previa.
  - `PROTOCOL_VERSION` no cambia: el digest distingue versiones del plugin; la versión del protocolo sigue siendo solo la del formato de los mensajes.
  - **Actualización coordinada:** un bridge ya instalado deja de conectarse hasta reinstalarlo con la versión que corresponde a este Sky-Claw (la razón queda en el log del broker y en el error de espera).
  - **Tests:** `tests/test_mo2_bridge_identidad_del_bundle.py`; el ancla enumera todos los archivos del bundle real (uno nuevo entra solo) y el único literal de `hello` del bridge debe declarar el digest. **No verificado:** el smoke en un MO2 real (instalar, alterar un archivo, ver el rechazo, reinstalar y reabrir).
  - **Fuera de este cambio, con nombre:** la tarjeta de estado de la GUI que muestre la identidad (H-17) va con P-3c.
- **El MO2 bridge entrega sus eventos en orden aunque el envío falle y tenga que reconectar (B-2, H-14).** Un evento cuyo envío fallaba se re-encolaba al **final** de la cola: con `[launch_ack, worker_exit]` y un fallo en el primero,
  tras reconectar salía `worker_exit` antes que `launch_ack`, y el broker (que arma su máquina de estados con ese orden) veía la salida de un worker que todavía no había confirmado. Ahora el evento que falló vuelve al **frente**
  (`BridgeEventOutbox.requeue_front`) y el drenaje vive en una sola función, `runtime.flush_outbox`, el único consumidor del outbox.
  - Se re-encola ante **cualquier** error del envío y no solo `OSError`/`VfsFrameError`: antes otro error perdía el evento terminal y dejaba la cuenta de pendientes sin saldar (el apagado habría esperado de más o dado el evento por enviado).
  - El drenaje salió de `plugin.py` (que importa `mobase` y no se puede cargar en tests) a `runtime.py`, así que la reconexión se prueba con un enlace falso que falla y se recupera.
  - **Cerrar MO2 a mitad de una corrida puede dejar la instancia en cuarentena, y es lo esperado:** el bridge termina el worker y drena su cola hasta 2 s, pero si el broker no alcanza a recibir `worker_exit` no puede probar que el proceso terminó y aísla la instancia (y veta el rollback). Se libera con `--mode vfs-quarantine` (H-4); no es un fallo a corregir en el bridge.
  - **Actualización coordinada:** el cambio vive en el bundle del plugin; hay que reinstalarlo (`--mode install-vfs-bridge`) y reabrir MO2 para que tenga efecto.
  - **Tests:** `tests/test_mo2_bridge_orden_de_eventos.py` (reconexión con fallos, productor concurrente con enlace intermitente que exige «lo entregado es exactamente lo producido y en su orden», y anclas por AST: `flush_outbox` es el único que saca eventos y solo él los mete al frente; el plugin solo toca el outbox con `put`/`wait_until_drained`). **No verificado:** una reconexión real contra MO2.
- **El razonamiento `<thought>`/`<think>` del modelo ya no llega al usuario en ninguna superficie (#918).**
  El `SYSTEM_PROMPT` pide ese bloque «oculto al usuario», pero nada lo ocultaba: Telegram lo rechazaba como HTML
  inválido («error interno») y la GUI, la web, el CLI y el WS lo mostraban crudo. `LLMRouter.chat` aplica ahora
  `ocultar_razonamiento` (`sky_claw/app/agent/reasoning_filter.py`) en sus dos retornos de texto final, después del
  guardrail de salida, y se retira el filtro propio de Telegram (`quitar_razonamiento`) para que haya un solo dueño.
  El filtro **falla cerrado**: los bloques se emparejan con una pila (un cierre de otro tipo no cierra el bloque), una
  apertura sin cierre oculta hasta el final y, si la respuesta era solo razonamiento, se muestra un aviso neutro en
  vez de su contenido. Las etiquetas dentro de código Markdown también se ocultan (decisión documentada: respetar los
  backticks abriría un bypass). En el modo de tool calling XML el parser solo ve el texto visible: una `<tool_call>`
  escrita dentro del razonamiento ya no se ejecuta. El reintento en texto plano del sender ante el 400 por entidades
  se mantiene como red de seguridad. Anclas: `tests/test_router_oculta_razonamiento.py` (enumera por AST todo `return`
  de `chat()`) y `test_el_filtro_de_razonamiento_tiene_un_solo_dueno` en `tests/test_reasoning_filter.py`.
- **Auditoría independiente de la pila del fence VFS (H-01, H-02, H-03; H-04 y H-05 quedan pendientes).** (H-01) El bridge del plugin contiene a los descendientes del worker que alcanza, no solo al proceso que lanzó MO2: tras asignar adopta a los descendientes vivos que no estén en el Job (hasta un punto fijo; si alguno no se puede contener no hay `launch_ack`, y si queda vivo fuera del Job —no se pudo abrir ni terminar— se reporta como terminación y el broker cuarentena la instancia) y emite `worker_exit` solo con el Job vacío confirmado (si no, `bridge_error` de terminación y cuarentena), porque el broker toma `worker_exit` como terminalidad confirmada. (H-02) `--vfs-worker` verifica su Job con un módulo liviano (`vfs_arranque`) ANTES de importar `vfs_worker` (cuyo árbol de imports llega a `platform` → `cmd.exe /c ver`); el aborto por identidad es uno solo para ambos puntos de entrada. (H-03) La limpieza de descendientes tras un fallo de identidad es por PID y no se corta al primer error. El HOLD de seguridad se mantiene y solo lo levanta el auditor independiente; detalle y límites (capa `ctypes` sin medir en Windows) en [docs/audits/2026-11-03_identidad_del_job_onefile.md §12](docs/audits/2026-11-03_identidad_del_job_onefile.md).
- **La tarjeta de Wrye Bash de la GUI lee su ruta de `config.toml` (`wrye_bash_exe`) como LOOT, xEdit y Pandora (#879).**
  El escáner de la GUI se siembra desde la config (`build_tool_path_cfg_keys`) y recién después exporta lo
  resuelto a `os.environ`; el `ExternalToolSpec` de `wrye_bash` tenía `snapshot_env_var` pero no
  `config_field`, así que con el ejecutable fuera de las rutas auto-detectadas la tarjeta decía «No
  instalado» y la etapa 6 asistida era inalcanzable desde la GUI (hallazgo H-1 del smoke en rig Windows,
  2026-11-07). Se agrega `wrye_bash_exe` a `Config._load_defaults`, a `LocalConfig` (+ lector JSON), a la
  migración legacy y a las dos tablas de docs. Ancla por enumeración en
  `tests/test_wrye_bash_config_field.py`: toda spec con `snapshot_env_var` tiene `config_field` o figura en
  una lista cerrada por nombre (hoy sólo `dyndolod`, fuera de alcance: su disponibilidad en la GUI es una
  decisión de la etapa 9), y todo `config_field` existe en `Config` y en `LocalConfig`.
- **Etapa 6 — vigencia contextual del testigo durable, simetría M-04 y robustez del almacén
  ([ADR 0016](docs/adr/0016-etapa-6-wrye-bash-asistida.md) §7.3, issue #866).**
  - **H-1:** si LOOT (u otro cambio de load order o de `modlist.txt`) ocurría entre el build del
    operador y la verificación, `preparar` y `verificar` seguían comparando contra el testigo viejo y
    declaraban `frescura_probada=True` sobre un parche anterior al cambio. El registro durable guarda
    ahora `huella_contexto` (SHA-256 de `plugins.txt` **sin** la entrada del propio Bashed Patch + `modlist.txt`);
    si no coincide y el artefacto apareció o cambió su digest, `preparar` abre una época nueva sin afirmar un build (con el artefacto sin cambios conserva la generación y solo actualiza la huella) y `verificar` no usa el testigo
    (nuevo campo `testigo_contexto`: `coincide` / `cambio` / `no_verificable` / `no_aplica`). Los registros
    de PR D se leen como contexto desconocido. El payload `testigo_previo` no cambia. Sin TTL por reloj.
    Un payload con `generacion` cuyo registro ya no existe (reemplazado, consumido o almacén ilegible)
    falla cerrado (`no_verificable`): repetir una verificación con un payload ya consumido deja de probar frescura.
  - **H-2:** `generate_bashed_patch` del agente LLM ahora corre el guard M-04 como la GUI y el
    `SupervisorAgent` (antes pasaba `validate_limit=False` con >254 plugins). Ancla por enumeración de los
    sitios productivos del servicio.
  - **H-3:** las anclas del retiro headless congelan también las funciones de módulo de
    `wrye_bash_service.py` y prohíben `subprocess` / `create_subprocess_*` / `Popen` / `WryeBashRunner`.
  - **H-4 / L-2 / L-3:** `os.replace` del almacén reintenta ante una violación de uso compartido
    transitoria en Windows (`winerror` 5/32), todo fallo de E/S sale como `AlmacenDeTestigosIlegibleError`
    y se barren los `.etapa6_testigos.*.tmp` huérfanos bajo el lock.
  - **L-4:** corregida la cifra del retiro (1231 → 676, −555 netas).
- **`add_mod_to_modlist` veía el mismo mod como dos si cambiaba una mayúscula (#838).**
  La primitiva comparaba los nombres del `modlist.txt` de forma exacta, mientras
  `ganadores._exigir_enabled` los compara con `lower()` porque MO2 resuelve
  `mods/<Nombre>` contra NTFS, que no distingue mayúsculas. Con `+x` ya en el perfil,
  `add_mod_to_modlist("X")` no lo reconocía como presente: agregaba `+X` y dejaba un
  perfil que el diagnóstico de overlay rechaza entero («el modlist repite el mod»), sin
  aviso —el WARNING de #835 sólo contaba coincidencias exactas—. Decisión (#838):
  **`X` y `x` son el mismo mod**. Los tres mutadores del modlist
  (`add_mod_to_modlist` —sus dos ramas y las dos formas de pedir máxima prioridad—,
  `remove_mod_from_modlist` y `toggle_mod_in_modlist`) comparan ahora con una única clave,
  `vfs._clave_de_nombre_de_mod`, que usa `lower()` y **no** `casefold()`: `casefold`
  colapsa `Straße`/`Strasse`, que NTFS no unifica, y fabricaría un «ya presente» (o un
  duplicado) que no existe —el mismo límite que `vfs_attestation._case_insensitive_key`
  y `tests/test_mo2_ganadores.py`—. Lo que se conserva es la **grafía** ya escrita:
  `-x` se habilita como `+x`, y el WARNING de duplicados nombra las variantes
  encontradas (`+x, +X`). Los callers no se tocan: el cambio vive en la primitiva y
  alcanza a `external_tools._activar_en_modlist` (NGIO, Community Shaders),
  `system_tools` (FOMOD), `ritual_runner` (GUI) y `grass_profile`
  (`highest_priority=True`). Ancla:
  `tests/test_mo2_modlist_identidad_sin_mayusculas.py`, que enumera los caminos de la
  primitiva (dos ramas, dos alias, `remove`, `toggle`), el límite `lower` vs.
  `casefold`, la consecuencia de punta a punta sobre `indexar_ganadores` y, por
  enumeración, qué métodos del modlist comparan por clave y cuáles son los callers.
- **Una corrida de TexGen que el operador autorizó continuar se revertía entera por
  el `Error:` de un modal de arranque CONOCIDO (F1).** El binario escribe la
  instrucción de sus diálogos de arranque como `Error: <instrucción>` en su propio
  log, y la taxonomía del veredicto (`dyndolod_runner.NO_FATALES_DEL_DOMINIO`) exentaba
  a uno de los dos modales medidos y no al otro: `dyndolod_dll_ng_ausente` quedaba
  cubierto por la categoría `("DynDOLOD.DLL", "not found")`, y
  `texgen_stitched_lod_previo` —`Error: Found stitched object LOD textures from
  earlier TexGen generation installed in game folder.`— caía en `_ERROR_SUELTO` y
  se clasificaba terminal. En el rig del 2026-10-19 eso hizo fallar y revertir una
  corrida que había generado 1.362 archivos / 138 MB, escrito `TexGen completed
  successfully` y salido con código 0, con el `Ignore` del modal ya aprobado por
  HITL; afectaba por igual a los dos modos, porque `clasificar_log` no recibe el
  modo. La entrada nueva es la instrucción **COMPLETA** del modal y no un prefijo
  (`("Found stitched",)` habría exentado también un fallo de escritura de las
  mismas texturas), y lleva el `Error:` del encabezado: como el match es por
  substring, sin él una línea con texto de error AJENO delante de la instrucción
  —`Error: Could not write textures; Found stitched object LOD textures …`— se
  tragaba como ruido de dominio y, con artefacto, marcador y `rc == 0`, el runner
  aceptaba la corrida (revisor CodeRabbit, PR #793). La severidad terminal sigue
  ganándole: `Critical:`/`FAIL:`/`Fatal:` sobre esa misma línea siguen siendo
  terminales. El ancla que impide el
  tercer modal es enumerativa sobre `CONTRATOS_DE_UI`
  (`test_todo_modal_conocido_del_contrato_es_no_fatal_en_el_log_de_su_herramienta`):
  un modal nuevo en el contrato rompe el test hasta que se le escriba su categoría,
  en vez de esperar a que tumbe otra corrida. **No** se retiró el modal del
  contrato —fingerprint 3/3 y `Ignore` funciona— ni se tocaron el presupuesto de
  timeout, `-memory`, los buffers de stdout/stderr ni el driver. La validación en
  rig Windows real sigue pendiente: este cambio se probó con el log real
  transcripto, no con una corrida nueva.
- **La etapa 9 desatendida abortaba sin escribir el Output si UIA leía la ventana
  mientras se repintaba (F2b, #781).** Hermano de F2 (#773) por la otra operación
  mutante: `_paso_escribir_output` tomaba UNA sola instantánea antes de escribir
  el campo Output, así que un `CARGANDO`/`SIN_VENTANA` transitorio —o una lectura
  que moría con `ObservacionUIAError` por un elemento stale a mitad de repaint—
  abortaba el paso aunque la pantalla contractual volviera un instante después.
  La reobservación vive dentro del MISMO paso y reutiliza el presupuesto de F2
  (`reobservacion_timeout_segundos`, 15 s, más el tope de 16 lecturas), pero NO
  copia el contrato del Invoke: la pantalla requerida es siempre `ASISTENTE`, no
  hay precondiciones propias y hay readback posterior. En cada vuelta se
  revalidan pid, ruta canónica del ejecutable, pantalla, unicidad y habilitación
  del campo Output; `backend.escribir_output` se ejecuta como máximo UNA vez y
  su desenlace —HRESULT fallido o readback ilegible— no se reintenta, porque la
  mutación pudo haber ocurrido. El plazo se verifica pegado a la primitiva con
  `reloj() >= limite`: en el instante de vencimiento no se escribe. Sólo se
  releen huecos de observación; otro modal, un diálogo desconocido, `AMBIGUA`, un
  control duplicado o deshabilitado y `UIANoDisponibleError` cortan en la PRIMERA
  lectura. **El hermano que F2 dejó sin cablear:** `_converger_output` pedía el
  paso con `observacion_intervalo_segundos` (1 s) mientras `_invocar` ya recibía
  el presupuesto de reobservación desde #773, así que aun con la reobservación
  implementada el hueco no entraba. Un desenlace desconocido tras la posible
  escritura deja de reportarse como `UIA_UNAVAILABLE` —que afirmaba "no pude
  mirar" sobre un paso que quizá ya mutó el campo del usuario— y pasa a
  `UNATTENDED_CONTRACT_VIOLATION` con `la escritura pudo haberse ejecutado y no se
  reintenta`. Son DOS los desenlaces que ya no caen en `UIA_UNAVAILABLE`: el de
  arriba y el del plazo vencido antes de escribir, que responde
  `UIA_CONTROL_MAP_MISMATCH` —el mismo que ya usa `_esperar_asistente` cuando
  vence su plazo de arranque— en lugar de caer al fallback `UIA_UNAVAILABLE`, que
  describía como "no pude mirar" un paso que no escribió nada. Los desenlaces
  restantes conservan el código que ya tenían y el mapa completo está enumerado en
  `test_f2b_el_mapa_de_codigos_de_la_escritura_enumera_todos_los_estados`.
  Validado con 13/13 mutantes muertos (sin reobservación, dos escrituras por
  readback ilegible, sin guard de deadline, `>=` por `>`, sin identidad, pantalla
  desconocida recuperable, HRESULT que reintenta, hermano sin presupuesto,
  desenlace desconocido mudo, excepción no recuperada, readback mentiroso, tope de
  lecturas infinito y plazo agotado cayendo al fallback genérico).
  *No hay validación en rig Windows: la corrección se probó con backends
  simulados deterministas y relojes falsos.*
- **La etapa 9 desatendida abortaba cuando UIA leía la ventana mientras se
  repintaba (F2, #773).** En la corrida D1 del rig Alpha-209 el operador aprobó el
  modal conocido de DynDOLOD DLL NG y el conductor cortó con
  `UIA_CONTROL_MAP_MISMATCH`: `_paso_invocar` tomaba UNA sola instantánea y esa
  lectura cayó en `pantalla=CARGANDO` con `invocado=False`; D2, con el mismo
  código, pasó. Reintentar desde el conductor no era una opción —`_invocar`
  reserva la acción en el ledger ANTES de pedir el paso, así que la segunda
  llamada es `UNATTENDED_CONTRACT_VIOLATION` por diseño— y la reobservación vive
  ahora dentro del MISMO paso `INVOCAR`: si la instantánea no muestra ninguna
  pantalla contractual (`CARGANDO`, `SIN_VENTANA`) se vuelve a leer con
  presupuesto finito (`reobservacion_timeout_segundos`, 15 s, más un tope de 16
  lecturas), revalidando en cada vuelta pid, ruta canónica del ejecutable,
  pantalla requerida, identidad del modal, precondición de Output y
  unicidad/habilitación del control. `backend.invocar` se ejecuta como máximo una
  vez y su desenlace —incluido un HRESULT de error— no se reintenta. Una pantalla
  decisiva DISTINTA de la esperada (otro modal, diálogo desconocido, `AMBIGUA`,
  control duplicado o deshabilitado) corta cerrado sin releer. El mismo hueco
  tiene una forma en excepción: una lectura que levanta `ObservacionUIAError`
  (elemento stale a mitad de repaint) también se relee con ese presupuesto,
  mientras que `UIANoDisponibleError` —el sensor— sigue cortando en la primera
  lectura, la misma distinción que ya hacía la observación; si no hubo ni una
  sola lectura legible el veredicto es `UIA_NO_DISPONIBLE`, no la pantalla. El
  presupuesto acota también la ACCIÓN, no sólo la espera: si la lectura que
  habilita el Invoke llega a `timeout_segundos` o después —una lectura
  lenta, con el modal contractual a la vista y la instantánea válida— el paso
  devuelve `PLAZO_AGOTADO` con `invocado=False` en vez de mutar fuera del
  presupuesto que el operador aprobó. El borde es `reloj() >= limite`: el
  presupuesto se agota EN el límite, la misma convención que la observación y
  las esperas del cierre. Es deliberadamente más estricto que la
  observación: observar de más no muta, invocar de más sí. El
  corte por identidad, además, tenía dos códigos según el camino: `_atender_modal`,
  `accion_previa`, Begin y terminal reportaban `IDENTIDAD_NO_COINCIDE` como
  `UIA_CONTROL_MAP_MISMATCH`/`TERMINAL_CONTROL_MISMATCH` mientras la observación y
  la escritura del Output ya usaban `UNATTENDED_CONTRACT_VIOLATION`; ahora los
  siete sitios comparten un único mapeo. Anclas:
  `tests/test_dyndolod_uia_driver.py` (casos A–G de #773, el caso I de la lectura
  ilegible, el caso J del presupuesto vencido —pantalla transitoria, excepción y
  el borde exacto del vencimiento con su contracara un instante antes—,
  el censo parametrizado de los cuatro caminos del corte por identidad y
  el test que fija que el padre pide el Invoke UNA vez aunque el paso corte sin
  invocar) y `tests/_dyndolod_gui_simulada.py`, que gana el guion de instantáneas
  —y de lecturas que fallan— con el que se reproduce el hueco de repintado.
- **El adapter legacy de `run_xedit_script` devolvía un resumen vacío como si fuera
  el resumen validado (#549).** La tool del agente lee `list_all_conflicts.pas` y
  verifica que `SUMMARY|total_conflicts` exista y cierre contra los conflictos
  parseados; un adapter heredado que entrega un `XEditResult` preparseado sin
  `raw_stdout` salteaba esa verificación y devolvía `success: true` con `conflicts`
  llenos y `summary: {}` — un dict vacío que cualquier consumidor que lea
  `summary.get(...)` confunde con un protocolo que declaró cero, cuando en realidad
  no hubo protocolo. Ahora esa rama declara la ausencia: `summary` es `None`, la
  clave sigue siempre presente y la forma del payload no cambia entre rutas. No se
  deriva `total_conflicts` de `len(conflicts)`: el conteo es la afirmación de xEdit
  de que el script llegó a `Finalize`, no un recuento nuestro. Se verificó que
  ningún caller productivo cae en la rama legacy —el único `run_script` de
  producción, `XEditRunner`, construye su resultado con `XEditOutputParser.parse`,
  que siempre conserva `raw_stdout`—, así que el cambio es de contrato sobre
  adapters heredados y no toca la ruta estricta. Anclas:
  `tests/test_xedit_agent_readonly_policy.py` (las dos ramas y los tres fallos
  cerrados) y `tests/test_xedit_summary_contract_invariant.py`, que descubre por
  AST el censo de funciones que llaman `parse_summary_line`, lo congela por igualdad
  literal y exige a cada una un guard `if clave not in resumen: raise` — la hermana
  del orquestador (`ConflictAnalyzer.analyze`) ya fallaba cerrado; el censo es lo que
  impide que un tercer consumidor herede la forma ambigua.
- **Un `open()` fallido dejaba al consumidor de SQLite "abierto" sin schema (#474).**
  `DistributedLockManager.initialize()` en modo standalone asignaba `self._conn`
  antes de los PRAGMA y el DDL: si algo fallaba, el sentinel
  `if self._conn is not None: return` convertía el reintento en un no-op sobre
  una conexión sin tablas, y la conexión quedaba abierta y sin dueño. #473 lo
  había arreglado sólo en la rama lifecycle y sólo para `OperationalError`. Los
  hermanos tenían la misma forma: `LLMRouter.open()` (standalone, idéntico),
  `AsyncModRegistry.open()` (DDL final fuera de todo `try`, en ambas ramas),
  `OperationJournal.open()` (limpiaba sólo ante `sqlite3.Error/JournalError`
  y antes del sweep: una cancelación u `OSError` dejaban `_db` puesto) y `ModRegistry.open()`
  (`except Exception` no ve la cancelación). Ahora la conexión se publica recién
  cuando la apertura terminó entera —incluido el sweep de arranque en `OperationJournal`—
  y cualquier `BaseException` la cierra (si es propia) y despeja el sentinel;
  `JournalConnectionError` sigue reservado a errores SQLite. Ancla:
  `tests/test_db_open_sentinel_invariant.py` detecta la familia por AST (método
  async con sentinel que abre SQLite), la congela contra sus recetas y recorre
  consumidor × rama (standalone/lifecycle) × fallo (SQLite, no-SQLite, cancelación),
  verificando sentinel despejado, conexión propia cerrada, conexión del lifecycle
  intacta, reintento funcional y robustez ante fallos en sweep.
- **El `--help` declaraba defaults que no eran los que corrían (#665).** `--mode`
  decía `(default: cli)` también en el `.exe` congelado, donde el default real es
  `gui` (un `set_defaults` posterior lo pisaba sin tocar el texto). El mismo
  defecto tenía su hermano en el parser: `--provider` decía `(default: deepseek)`
  aunque el default efectivo sale de `llm_provider` en la config; `--vfs-timeout`
  decía `30` frente a un `30.0` efectivo, y `--profile` usaba el formato
  `(default: …)` para describir una resolución posterior (su default argparse es
  vacío). Ahora el default de `--mode` se decide una sola vez y todos los
  `(default: X)` se derivan con `%(default)s`. Ancla:
  `tests/test_cli_help_defaults.py` recorre TODAS las acciones del parser en los
  cuatro contextos (fuente/congelado × config con/sin provider) y compara cada
  default declarado contra `parse_args([])`; el conjunto de argumentos que
  declaran default queda congelado por igualdad literal.
- **El launcher `SkyClawApp.bat` no encontraba el entorno del repo y avisaba por
  el puerto equivocado.** Buscaba `venv\Scripts\python.exe` (el repo usa `.venv`,
  el que crean `build.bat`/`uv`) y caía al Python del sistema, que no tiene
  `sky_claw` instalado → `ModuleNotFoundError`; ahora usa `.venv\`, el mismo
  criterio que el ancla de `build.bat` en `tests/test_pyinstaller.py`. El chequeo
  de puerto miraba 8888, que no lo bindea nadie: la GUI usa 8080 (NiceGUI) y
  8765 (API `/api/chat`), así que el `WinError 10048` real al reabrir pasaba sin
  aviso. De paso, los mensajes `echo [!]` del launcher (y el `Build complete!`
  de `build.bat`) perdían el `!` al imprimirse —con `enabledelayedexpansion` el
  parser lo toma como inicio de variable y lo descarta, así que el aviso salía
  `[] ALERTA`—; ahora usan tags sin `!`. Ancla:
  `tests/test_launcher_usuario.py` (los puertos avisados se comparan contra los
  que bindean `gui_mode.py`/`_bootloader.py`, y TODOS los `echo` de ambos `.bat`
  se recorren buscando `!` sueltos).
- **La CLI anunciaba `exit`/`quit` pero no los implementaba.** El REPL sólo salía
  con EOF (`Ctrl+D`) o `Ctrl+C`, así que `exit` viajaba al LLM como un mensaje
  más. Ahora cierra con `exit`, `quit` o `salir` (con `/` inicial opcional y sin
  distinguir mayúsculas), el banner se deriva de la misma constante y la familia
  se congela en `tests/test_cli_repl_comandos.py`.
- **GUI — pulido visual del shell Forja del Dovahkiin.** El sidebar, el toggle
  «Confirmar», las filas del Orden de Carga, las tarjetas de ritual y las
  filas de Disputas se renderizaban desarmados (ícono arriba, etiqueta abajo)
  porque `ui.html` envuelve su contenido en un `<div>` que queda fuera del flex;
  ahora esos fragmentos usan `display:contents`. El CTA del wizard y los
  selectores de proveedor IA recuperan el oro del tema (los `!important` de
  Quasar viven en una capa CSS que vence a los del tema). El shell pasa a alto
  fijo con scroll interno (Vitalidad del Sistema siempre visible), el chat del
  Asistente es legible sobre el pergamino, la grilla de rituales queda 4+3, el
  hero gana filigrana SVG en sus esquinas y el header no desborda en ventanas
  angostas. Anclas nuevas en `tests/test_gui_theme_contracts.py`.
- **xEdit headless: el borde con SSEEdit no funcionaba en un rig real.**
  `run_script` y el comando de escritura no pasaban `-autoexit` (xEdit quedaba
  abierto hasta el timeout), apuntaban `-D:` a la raíz del juego en vez de a
  `Data` y el de escritura no pasaba `-autoload`. El protocolo `AddMessage`
  (`CONFLICT|`/`SUMMARY|`/`DUMP_*`) ahora se lee del log `-R:<archivo>` (xEdit
  es un binario GUI sin stdout) y `ConflictAnalyzer.analyze` / el tool
  read-only fallan cerrado sin `SUMMARY` consistente en vez de reportar
  "0 conflictos". `execute_patch` verifica que el plugin de salida exista en
  `Data` tras exit 0. Scripts Pascal: `AddNewFileName` en lugar de
  `AddNewFile(nombre)`, `wbCopyElementToFile` + `AddRequiredElementMasters` en
  lugar de `wbCopyElementToRecord`, sin locales que oculten funciones de xEdit
  (`formID := FormID(e)`), el merge estático ya no toma el nombre de salida de
  `ParamStr` y `list_all_conflicts.pas` descarta cadenas ITM/benignas
  (`ConflictAllForMainRecord < caOverride`). Ancla:
  `tests/test_xedit_headless_contract.py`. Tras la review: QuickAutoClean también
  lee el log `-R:` (su parser de errores veía un stdout vacío), el post-check
  exige que el plugin haya cambiado (`mtime_ns`, tamaño) y no solo que exista,
  el log se decodifica como UTF-8 con fallback ANSI (`mbcs`/cp1252) y el
  nombre de salida del merge estático se alinea con el plan
  (`SkyClaw_MergedPatch.esp`). Pendiente: smoke en rig real con SSEEdit.
- **MO2/USVFS — fingerprint de perfil estable ante la reserialización de `plugins.txt` al lanzar
  (#633).** El rig real de PR-586C falló cerrado porque `IOrganizer.startApplication` reescribe
  `plugins.txt` (header, CRLF, encoding System, sin BOM, sin masters oficiales) entre el preview y
  el worker: 141 B → 96 B con el estado del perfil intacto. `_profile_fingerprint` pasa a hashear
  `plugins.txt` por su **estado semántico** (plugins habilitados no oficiales, en orden) en el
  dialecto Creation/SSE probado en `CreationGamePlugins::readPluginList`/`writePluginList`, y solo
  tolera lo demostrado como neutro: comentarios, líneas vacías, espacios, BOM, LF/CRLF/CR, líneas
  deshabilitadas (el lector trata "deshabilitado" y "ausente" igual) y líneas de todo
  `primaryPlugins()` —masters base **y** el contenido de Creation Club que el juego declara en
  `Skyrim.ccc`—, que se fuerzan `ACTIVE` y el escritor omite (sin asumir por prefijo `cc`: un mod
  `ccAlgo.esp` no declarado sigue siendo estado). Los nombres se validan como nombres de archivo de
  Windows (encoding `System`), así que los bytes no ASCII y los espacios interiores —p. ej.
  `Unofficial Skyrim Special Edition Patch.esp`— son válidos; solo falla cerrado lo indeterminable
  (control/NUL, metacaracteres, sin extensión de plugin, repetido case-insensitive), y
  `modlist.txt`, `loadorder.txt` y los `settings.*` siguen crudos. La identidad que entra al
  payload canónico es la clave normalizada case-insensitive, no la grafía del archivo: en un
  filesystem case-insensitive `*X.esp` y `*x.esp` son el mismo plugin, y hashearlos distinto
  volvía a atar el digest a una reescritura sin significado. El digest sube de dominio a `skyclaw-vfs-profile-v2` y las secciones
  llevan largo explícito (el esquema v1 permitía colisionar dos repartos de bytes distintos).
  Anclas: `tests/test_vfs_attestation_canonicalization.py`.
- **Tres rituales destructivos devolvían una excepción donde el contrato promete un dict (#726).**
  `generate_animations` (Pandora), `quick_auto_clean` (SSEEdit) y `generate_bashed_patch`
  (Wrye Bash) se registraban en el dispatcher sólo con el gate HITL, sin
  `ErrorWrappingMiddleware` ni `DictResultGuardMiddleware` — el hueco que #722 cerró únicamente
  para `generate_lods`. Consecuencia: el `ValueError` con que `validate_for_approval` rechaza un
  payload con claves que la estrategia no honra (p. ej. un `dry_run=True` que el operador aprueba
  y la estrategia ignora) escapaba del dispatcher hacia la GUI en vez de degradar al contrato
  legacy, y un resultado no-dict rompía `normalize_tool_result`/`summarize_ritual_result`, que
  asumen `dict`. Ahora las tres llevan la misma tripleta que `generate_lods`: excepción →
  `{"status": "error", "reason": <código propio>, "details": ...}`, y resultado no-dict → el
  código del guard. Alcance verificado leyendo cada servicio, no asumido: `quick_auto_clean` es
  el hueco MEDIDO —`XEditPipelineService` sólo captura `_ActionManifestError`,
  `LockAcquisitionError` y `(PatchingError, XEditError)`, sin el `except Exception → dict` de la
  Regla T11 que sí tienen Pandora y Wrye Bash—; en esas dos el wrapping es defensa en profundidad
  y uniformidad del contrato del dispatcher, no la corrección de un escape observado. Se preserva
  lo que ya era correcto: la cancelación sigue propagándose sin envolver, el estado terminal
  sigue siendo `FAILED` y la denegación HITL no queda tapada por el wrapping. Superficie hermana
  auditada (no tocada): el registry del agente LLM ya envuelve sus dos tools equivalentes
  (`run_pandora`, `generate_bashed_patch`) y no expone `quick_auto_clean`. Anclas:
  `tests/test_destructive_strategies_error_contract.py` — 26 casos más un ancla de familia por
  introspección sobre `DESTRUCTIVE_TOOL_PATTERNS`.

### Added
- **Cooldown COMPARTIDO del flood-control de Telegram (#706).** El reintento de un
  HTTP 429 dormía **sólo** a la corrutina que lo recibió: una `send()` concurrente
  pasaba el rate limit local de inmediato (un 429, por contrato, no registra envío) y
  volvía a golpear la API **dentro de la misma ventana** que Telegram acababa de
  anunciar, agotando los reintentos acotados de la ráfaga y perdiendo avisos. Ahora
  `TelegramSender` publica cada 429 en `CooldownCompartido` —deadline monotónico,
  extendido con `max` para que dos avisos concurrentes converjan en el más lejano—
  y lo consulta **antes de cada intento HTTP**, no sólo antes del primero. La autoridad
  retiene la restricción íntegra del servidor (tope de sanidad de 24 h) —distinguiendo
  el umbral de reintento de 30 s de la duración del deadline— mientras los
  llamadores operan con presupuestos finitos (90 s por defecto) para fallar rápido
  determinísticamente sin golpear la red. La espera reevalúa en bucle tras cada suspensión,
  honrando extensiones progresivas de múltiples corrutinas. Los mensajes multichunk
  comparten un presupuesto acumulado de cooldown único reportando entregas parciales,
  descontando exclusivamente las suspensiones reales de flood-control sin penalizar
  la espera del rate limit local ni la latencia de red.
  `send_document` clasifica el agotamiento de presupuesto como `TelegramUploadTimeoutError`
  y las vías HITL (`editMessageText`, `answerCallbackQuery` con cota de 10 s) acotan su espera para evitar
  bloqueos prolongados, atrapando fallos de transporte y rate limit en el ack con inspección estricta de URLs HTTP y sin revertir decisiones comprometidas.
  El alcance es el del **bot** (un 429 no permite atribuir si el
  límite excedido fue el del chat o el global). Un 429 sigue sin ocupar un cupo del rate
  limit local ni producir identidad de mensaje, la espera es cancelable y no hay lock sostenido
  durante una petición. Anclas: `tests/test_telegram_flood_control_compartido.py`
  (reloj simulado y envíos concurrentes deterministas, más el ancla AST que enumera
  los caminos HTTP y exige la consulta previa a la petición) y
  `tests/test_telegram_sender_adjuntos_y_429.py`.
- **`sky_claw/local/AGENTS.md` — SOP canónico del pipeline de modding de Skyrim para agentes IA** (orden cronológico de stages xEdit → CAO → BodySlide → Pandora → LOOT → Wrye Bash → Synthesis → No Grass In Objects → TexGen/DynDOLOD, reglas por tool, conflict resolution protocol, critical failure modes, code-editing rules para agentes). Cubre los tres subsistemas gobernados: `sky_claw/local/tools/`, `sky_claw/local/xedit/` y `sky_claw/app/orchestrator/tool_strategies/`. Acompañado de **`sky_claw/app/orchestrator/AGENTS.md`** (pointer que redirige a la SOP para que los agentes que editen el dispatcher la descubran). Referenciados desde el `AGENTS.md` raíz. *Audiencia: cualquier LLM agent (Claude Code, Cursor, Aider, Gemini, Codex) que edite código del pipeline.*
- **Runtime Vault — global TGR serialization lock (GP2-P2).** Primitiva de
  exclusión cross-process que serializa los ciclos `LOAD → MODIFY → WRITE` sobre
  `trusted_goldens.json` y elimina el lost update entre writers privilegiados
  (atomic replace solo NO serializa read-modify-write). `trusted_registry_lock.py`:
  lock file abierto con `CreateFileW(dwShareMode=0, OPEN_ALWAYS)` en
  `%ProgramData%\Sky-Claw\runtime_vault\locks\skyclaw_tgr_lock_<sha256>.lock`
  (identidad derivada del registry path canónico — global al registry, no por
  Golden; prefijo y semántica distintos de `GoldenMutationLock`), ownership =
  vida del handle kernel (crash-safe: existencia del residual `*.lock` jamás
  equivale a lock activo; sin metadata persistente; nunca se borra ⇒ sin
  delete-race), reparse-check post-open, reentrancia con rechazo tipado, espera
  acotada configurable (`timeout` finito, poll dormido, BUSY/TIMEOUT fail-closed).
  API difícil de usar mal: `mutate_trusted_golden_registry(mutate)` (sin paths:
  load y atomic write ocurren estructuralmente bajo el lock) +
  `acquire_trusted_registry_write_lock()`. **Dos propiedades distintas:** el
  reemplazo atómico del TGR (P1, `_write_trusted_registry_atomically_at`) y la
  serialización global del TGR (esta primitiva) son invariantes separadas; este
  lock protege el RMW del registry y **no** congela el contenido del Golden ni
  cierra la ventana TOCTOU (`TGR_LOCK_PROTECTS_GOLDEN_CONTENT = NO`). Windows-only
  productivo (fail-closed tipado en POSIX). Sin semántica P3 (admission/refresh).
  Anclas: `tests/test_runtime_vault_trusted_registry_lock.py` (lost-update oracle
  real cross-process, contención, crash release, exception release, mutation
  anchor del orden acquire→load→modify→write→release).
- **Runtime Vault — Golden Admission / `REGISTER_OR_REFRESH_TRUSTED_GOLDEN` (GP2-P3).**
  Servicio backend que registra (ABSENT → B) o refresca (A → B) una entrada del
  Trusted Golden Registry, fail-closed, sin agregar estados al FSM de GP2 apply y
  sin escribir el TGR desde el path de apply. Fuentes cerradas del axioma de
  autoridad única (§11.0): `INDEPENDENT_PROVENANCE` (exige un provider de
  provenance explícito; sin provider cableado la operación termina
  `SOURCE_UNAVAILABLE`, `REJECTED` y con cero TGR writes) y `OPERATOR_TOFU`
  (fuente de producción). La expectativa admitida sale del provider, nunca de
  `request.expected_tree`/`expected_runtime`/`source_reference`, que son
  candidatos del caller. Un solo commit
  estructural: `mutate_trusted_golden_registry` (lock cross-process de P2) →
  `LOAD` → `mutate(current)` → replace atómico, con la distinción tipada
  `REJECTED` (no hubo replace: fallo previo → `TGR_CONCURRENT_CHANGE` /
  `STORE_FAILED` / `AUDIT_RECORD_FAILED` / `UNEXPECTED_FAILURE`) vs
  `COMMIT_OUTCOME_UNKNOWN` (el replace pudo ocurrir sin resultado legible →
  revalidación del TGR contra `before`/`after` exactos; nunca se reporta
  `REJECTED` ni se afirma que la fila previa quedó intacta). El claim del
  `operation_id` es one-use y su colisión se tipa
  (`GoldenAdmissionClaimAlreadyExistsError`), de modo que el perdedor de la
  carrera responde `REJECTED` sin `record_path` y nunca lee ni escribe el
  registro del ganador. TOFU sigue el orden
  de §11.4: `OBSERVE` (sólo `OBSERVED`) → confirmación privilegiada
  (`TOFU_PRE_RERUN`, con la advertencia
  `OPERATOR_TOFU DOES NOT DETECT PRE-EXISTING COMPROMISE`) → receipt one-use
  emitido por el helper → RV-2 fresco desde cero bajo el token original →
  `VERIFIED` → write TGR, con la confirmación de provenance
  (`PROVENANCE_POST_VERIFIED`) también antes del commit. Registro de auditoría
  protegido por `operation_id` (receipt inmutable, expectativas normalizadas,
  observaciones por pass con `ESTADOS_ADMITIDOS_POR_PASS`, binding TGR
  `before`/`after`) persistido y revalidado **antes** del replace; frontera de error
  del store sellada con `GoldenAdmissionStoreError` para toda falla operativa; append
  terminal de auditoría desacoplado del desenlace ya decidido (falla de auditoría
  post-commit preserva `REGISTERED` o `COMMIT_OUTCOME_UNKNOWN` con warning; el servicio
  nunca lanza excepciones operativas ordinarias); `registered_at`
  UTC ISO-8601 `Z` con microsegundos, único por root físico (+1 µs acotado o
  fail-closed). `critical_expectations` con serialización normativa exacta
  (`SHA-256(UTF-8("[]"))` para la lista vacía). Anclas:
  `tests/test_runtime_vault_critical_expectations.py`,
  `tests/test_runtime_vault_golden_admission.py`,
  `tests/test_runtime_vault_golden_admission_store.py`,
  `tests/test_runtime_vault_golden_admission_service.py`. Sin wiring a
  GUI/LLM y sin cambios a la CLI pública del helper (contrato §11.4).

### Security
- **OpenTelemetry sube al piso `>=1.45.2` por CVE-2026-126360 (`opentelemetry-api <=1.45.1`).** `pip-audit --strict` del job «Security Scan» detectó el aviso nuevo sobre `opentelemetry-api==1.45.1` y dejó en rojo a todos los PRs y a `main` (el lock es el mismo para todos). Se sube el piso en los tres paquetes declarados de `pyproject.toml` y se regeneran los TRES locks (`requirements.lock`, `requirements-native-parallax.lock`, `uv.lock`) con `--upgrade-package` solo sobre la familia: se mueven exactamente `opentelemetry-api`, `-sdk`, `-exporter-otlp-proto-grpc`, `-exporter-otlp-proto-common`, `-proto` (1.45.1 → 1.45.2), `-exporter-otlp-common` y `-semantic-conventions` (0.66b1 → 0.66b2), y ninguna otra dependencia. El ancla `test_opentelemetry_piso_de_seguridad_declarado_y_bloqueado` sube su piso a 1.45.2 y trata 1.45.1 como la versión vulnerable. Verificado con `pip-audit` sobre los dos `requirements*.lock`: antes, 1 vulnerabilidad (la de este aviso); después, ninguna.
- **`multidict` 6.7.1 → 6.9.1 (CVE-2026-104874)** — aviso que
  `pip-audit --strict --skip-editable -r requirements.lock` empezó a marcar
  en el gate "Security Scan". Dependencia transitiva vía `aiohttp`/`yarl`;
  se sube floor directo en `pyproject.toml` (`multidict>=6.9.1,<7`, mismo patrón de pin
  transitivo que `starlette`/`anyio`) y se regeneran `requirements.lock` **y**
  `uv.lock`. Sin cambios de código propio.
- **`anyio` 4.13.0 → 4.14.2 (CVE-2026-63374, CVE-2026-64847)** — avisos que
  `pip-audit --strict --skip-editable -r requirements.lock` empezó a marcar
  en el gate "Security Scan". Dependencia transitiva vía `starlette`/`httpx`/`watchfiles`;
  se sube floor directo en `pyproject.toml` (`anyio>=4.14.2,<5`, mismo patrón de pin
  transitivo que `starlette`/`python-multipart`) y se regeneran `requirements.lock` **y**
  `uv.lock`. Solo cambia ese pin (el grafo transitivo no varía), sin cambios de código propio.
- **`python-engineio` 4.13.1 → 4.13.3 (CVE-2026-48802, CVE-2026-48809) y
  `python-socketio` 5.16.1 → 5.16.3 (CVE-2026-48804)** — misma ola de avisos
  que `pip-audit --strict --skip-editable -r requirements.lock` empezó a marcar
  tras avanzar su base de advisories, poniendo rojo el gate "Security Scan"
  (pre-existente en `main`, ajeno al feature work). Ambas son transitivas vía
  `nicegui → python-socketio`; se suben floors directos en `pyproject.toml`
  (`python-engineio>=4.13.2`, `python-socketio>=5.16.2`, mismo patrón de pin
  transitivo que `starlette`/`python-multipart`) y se regeneran
  `requirements.lock` **y** `uv.lock`. Solo cambian esos dos pins (el grafo
  transitivo no varía), así que no requiere re-bundlear el exe.
- **Familia `langgraph`: `langgraph-checkpoint` 4.0.3 → 4.1.1
  (CVE-2026-48775) y `langgraph-sdk` 0.3.14 → 0.3.15 (CVE-2026-48776)** —
  divulgación coordinada que `pip-audit --strict` marcó en `requirements.lock`,
  poniendo rojo el gate "Security Scan". Se suben los floors en `pyproject.toml`
  (`langgraph-checkpoint>=4.1.1,<5`; `langgraph-sdk>=0.3.15`, transitiva vía
  `langgraph`) y se regeneran `requirements.lock` **y** `uv.lock`. Solo cambian
  esos dos pins (el grafo transitivo no varía), así que no requiere re-bundlear
  el exe.
- **Capacidad de xEdit del agente LLM restringida a read-only bundleado (#548).**
  La tool `run_xedit_script` que expone el agente pasa a ejecutar únicamente una
  allowlist de scripts de diagnóstico bundleados read-only (`xedit_readonly_tool.py`,
  `xedit_policy.py`): el schema Pydantic (`XEditAnalysisParams`) y el runtime comparten
  la misma política de nombres, un nombre desconocido falla cerrado en ambos, se exige
  pre-staging canónico (`ensure_scripts_staged()`) antes de ejecutar (el handler exige
  el método y lo invoca, pero ignora su resultado y ejecuta por nombre de archivo: es
  pre-staging, no provenance verificada — queda una ventana TOCTOU), y cada script se
  enruta a su parser/analyzer específico. No cambia las
  rutas mutantes de xEdit del orquestador (`XEditPipelineService`), que conservan su
  frontera con lock/journal/rollback. Ancla: `tests/test_xedit_agent_readonly_policy.py`.

### Changed
- **La suite completa de Windows se reparte en 4 shards por archivo, con una verificación de que ningún test quedó sin correr.**
  En un solo job la suite tardaba 50-72 min (py3.11 y py3.12) y cada push de cada PR reiniciaba esa espera; el propio
  comentario de `ci.yml` ya señalaba repartirla como el paso siguiente. Ahora el job `test-shard` (matriz Windows × py3.11/3.12 ×
  4 shards) corre `pytest` con el plugin `scripts/ci_particion.py`, que colecta la suite **entera** y se queda con los archivos que
  le tocan (reparto voraz por cantidad de tests, determinista y independiente del orden): no hay una lista de archivos que
  mantener, y un subdirectorio nuevo de tests queda cubierto por construcción (`tests/` ya tiene `agent/`, `bdd/` y `security/`).
  - **El riesgo de repartir es que un test no corra en ningún shard sin que nada falle.** Cada shard deja un manifiesto (archivos,
    cantidad de tests, huella de la suite colectada, tiempo de tests por archivo) y el job `test` —que conserva el nombre del check por
    versión, `🧪 Tests / windows-latest / py3.x`— exige que entre todos hayan seleccionado exactamente la suite: misma huella, mismo
    total, ningún archivo
    en dos shards, la suma de tests igual al total colectado y la huella declarada igual a la recalculada con los archivos que de verdad
    seleccionaron (un shard que declara otro archivo del mismo peso ya no pasa). Un manifiesto mal formado (conteo de texto, `true`, 0) se reporta
    como error con su ruta en vez de reventar con `TypeError`, y `--total` no positivo es un error de uso (antes `--total 0` sobre un directorio
    vacío daba OK). Un `SKYCLAW_SHARD` mal escrito aborta la
    sesión en vez de degradar en silencio a «correr todo».
  - **La cobertura sigue siendo un gate bloqueante** (`--fail-under=60`): se mide en cada shard y se exige sobre los datos combinados
    de todos (un shard solo cubre una parte); el verificador exige, con `--con-cobertura`, la base `coverage.sqlite` de CADA shard, presente y legible **por Coverage.py** con al menos un
    archivo medido (`coverage combine` sobre un glob con una base de menos no falla, y una truncada o sin su esquema la omite con una advertencia y exit 0:
    las restantes pueden igual cruzar el umbral; `PRAGMA quick_check` de SQLite no alcanzaba: una base sin la tabla `file` da `ok`). `coverage xml` corre antes del umbral para que el
    artefacto `coverage-report-*` exista aun si el umbral falla; conserva su nombre y su ruta.
  - **Un shard rojo deja rojo el veredicto** (no «skipped», que GitHub da por satisfecho en un check requerido). El agregador
    `🧪 Tests (Pytest)` y el job `Build` no cambian. Los artefactos intermedios duran 1 día: pasado ese plazo, «Re-run failed jobs» no sirve para
    reintentar solo el veredicto (los shards verdes no se repiten y sus artefactos expiraron); hay que usar «Re-run all jobs». Las dos subidas con
    `if: always()` (`Upload shard results` y `Upload coverage`) llevan `overwrite: true`: el artefacto queda subido aunque el job falle y, con el valor por
    defecto, el reintento del mismo shard chocaba con el nombre de su primer intento y quedaba rojo aunque pytest pasara (un sufijo con `run_attempt` no
    sirve: el verificador vería un shard repetido). Un test enumera toda subida con `always()` y congela ese censo.
  - **Anclas:** `tests/test_ci_particion.py` (propiedades del reparto enumeradas para N de 1 a 9 sobre casos sintéticos y sobre el árbol
    de tests real, cada modo de falla del verificador y el plugin ejercido en un subproceso) y `tests/test_ci_platform_policy.py`
    (la cantidad de shards atada a la matriz, a `SKYCLAW_SHARD` y al `--total`; el piso del `timeout-minutes` pasa a anclar el job
    que corre la suite; todas las acciones fijadas por SHA completo, incluido el nuevo `download-artifact`).
  - **Medido en la CI de su propio PR (Windows, UNA corrida):** los 8 shards verdes, de 9:28 a 14:57 min cada uno (py3.12: 9:28-12:12;
    py3.11: 10:48-14:57), el veredicto por versión en ~2 min y todo el run, hasta `Build` verde, en ~20 min contra los 60-72 min de la
    suite entera en un job. El reparto por cantidad de tests no equilibra perfecto (el shard más lento de py3.11 tarda ~4 min más que
    el más rápido); cada manifiesto registra el tiempo de los tests por archivo para calibrarlo con datos (es la suma de las fases setup/call/teardown: no incluye
    la colección ni el arranque, así que para decidir el `timeout-minutes` manda la duración del job entero que muestra Actions).
  - **El shell importa:** la primera versión corría `pytest` desde `bash` y dos tests que asumen un proceso sin privilegios
    (`test_tgr_unelevated_without_privilege_fails_closed` y `test_ns_unelevated_without_privilege_fails_closed`) fallaron con «DID NOT
    RAISE» en los shards 2 y 3; con PowerShell, el shell que usaba el job único, pasan los 8 shards. Se midió que el resultado cambia con
    el shell, **no por qué** (la hipótesis es el estado del token del proceso). Esos tests no establecen la condición que asumen:
    corregirlo es un seguimiento aparte, no un motivo para cambiar el shell.
  - **No verificado:** el `timeout-minutes` de los shards se conserva en 90 a propósito: una sola corrida no alcanza para bajar un piso
    que el historial muestra volátil (el runner lento de py3.11 llegó a +30 %). Tampoco se midió la concurrencia de runners con varios
    PRs a la vez ni la protección de rama (se asume, por el comentario de `ci.yml`, que exige los agregadores).
- **Política de plataformas de CI acotada a Windows para la suite completa (#546).**
  La suite `pytest` con coverage se certifica en **Windows** (Python 3.11 y 3.12) y es
  el único blocker duro; Linux queda como señal **POSIX acotada e informativa**
  (`test-posix-core`: Ubuntu, Python 3.11, `continue-on-error: true`, subconjunto de
  tests del núcleo async). Ancla: `tests/test_ci_platform_policy.py`.

### Fixed
- **VERSIONINFO del `.exe` ahora se embebe y se deriva solo** (`sky_claw.spec`).
  El spec pasaba un dict `version_info={...}` a `EXE(...)`, pero PyInstaller solo
  honra el kwarg `version=` (un `VSVersionInfo` o un path a archivo); el dict se
  descartaba en silencio, así que el binario se publicaba **sin recurso de
  versión** (Propiedades → Detalles vacío) y el "bump manual" de
  `(0, 2, 4, 0)` no tenía efecto. Ahora la tupla `(major, minor, patch, 0)` se
  deriva en build time de `importlib.metadata.version("sky-claw")` (el tag de
  `hatch-vcs`), con fallback a `sky_claw.__version__` y a `(0, 0, 0, 0)` si no
  parsea; los sufijos dev/dirty (`0.2.4.devN+g…`) se toleran vía regex. Se
  elimina el footgun del bump manual: `sky_claw/__init__.py:__version__` queda
  como **único punto manual** de versión.

## [0.2.4] - 2026-06-24

### Added
- **Hardening del lease en `SnapshotTransactionLock`** (`db/locks.py`) — defensa
  en profundidad para cerrar la ventana entre la pérdida real de un lease y su
  detección por el heartbeat (acotada hasta `TTL/renew_divisor`, ~200s con el
  default 600s/3). Nuevo `assert_owned(verify_db=True)`: API *opt-in* que los
  callers invocan justo antes de una mutación crítica para re-verificar
  exclusividad (fast-path por flag + chequeo fresco de propiedad contra la DB),
  achicando la ventana a ~0 (compara el token de adquisición `acquired_at`, no
  solo `agent_id`, para detectar readquisiciones de un mismo servicio). Nueva
  perilla `renew_divisor` (default 3.0, finito y `>= 2.0`; el intervalo tiene piso
  para no martillar la DB) para acortar el intervalo de renovación sin tocar el
  TTL; un lease perdido detectado por `assert_owned` toma el mismo camino
  sin-rollback que el del heartbeat. Ambos **aditivos y
  backward-compatible**; aún **no** cableados en call sites (se evita el
  cargo-cult: es una API opt-in que cada runner adopta solo donde haya una
  mutación cross-process real).

### Security
- **Redacción key-aware de secretos sin forma** (`logging_config.py`) — la capa
  `_SENSITIVE_KEY_RE` (que redacta el *valor* de un extra estructurado según el
  nombre de su clave, para secretos sin prefijo reconocible) cubría solo
  `aws_secret_access_key`. Se extiende a `client_secret` y
  `(?:bot|access|refresh)_token`, de modo que un `client_secret` en un log
  extra se redacta aunque el valor no tenga forma detectable por los patrones
  de texto — tanto en la forma estándar `extra={"client_secret": v}` (atributo
  top-level del `LogRecord`, el path común) como dentro de un dict anidado.
  Cada alternativa es un **nombre de clave completo** anclado
  con `\b`: se excluye `token` suelto a propósito para no clobberear la
  telemetría de token-budget (`token_count`/`max_tokens`/`prompt_tokens`), con
  test de regresión que lo fija.

## [0.2.3] - 2026-06-20

### Added
- **Chat GUI↔daemon (`/ws/ui`)** — la GUI ahora **conecta** al daemon y el chat
  responde con el LLM en vez de quedar en "⚠️ Daemon offline". Nuevo handler
  command-aware en `web/app.py` (`:8765`, junto a `/api/chat` y `/api/status`)
  que rutea `command/chat` → `LLMRouter.chat` y responde
  `{"type":"response",...}`, con auth `X-Auth-Token` (cierra con `4001` en
  rechazo) y el **test de round-trip GUI↔daemon que faltaba** (la causa de que
  el remap erróneo de #195 pasara verde). El remap `/ws/ui`→Operations-Hub se
  mantiene descartado (cuelga la UI). Alcance Q&A; el chat *agentic* (ejecutar
  herramientas desde el chat) queda para una feature futura con su propio diseño.

### Fixed
- **`langsmith` 0.8.3 → 0.8.18** (GHSA-f4xh-w4cj-qxq8). Dependencia transitiva
  (vía `langchain-core`); se fija un *floor* directo `>=0.8.18` en
  `pyproject.toml` (siguiendo el patrón ya usado para `starlette` y
  `python-multipart`) y se regeneran **ambos** lockfiles (`requirements.lock`
  y `uv.lock`), de modo que tanto la instalación vía pip como la vía uv
  documentada (`uv sync --frozen`/`--locked` en `setup_env.ps1` y
  `DEPLOYMENT.md`) quedan remediadas. Único hallazgo del `pip-audit --strict`
  tras avanzar su base de advisories; sin cambios de API. Al resincronizar
  `uv.lock` (stale desde #190) también se cierran en la vía uv los pines ya
  corregidos en `requirements.lock` (p. ej. `py7zr` 0.22.0 → 1.1.3).

## [0.2.2] - 2026-06-20

### Fixed
- **El runtime de `SupervisorAgent.start()` crasheaba en Windows localizado**
  (no-inglés, p. ej. es-ES) con `unable to open database file` en el DLQ —
  resuelve el *Known Issue* de 0.2.1. Dos causas en
  `security/file_permissions.py::_restrict_windows`:
  - El hardening pasaba **nombres en inglés** (`Users`,
    `BUILTIN\Administrators`, …) a `icacls /remove`; en Windows localizado no
    mapean a SID → exit `1332` → el fail-closed destruía el secreto aunque el
    `/grant` hubiera funcionado. Ahora se quitan por **SID well-known**
    (`*S-1-1-0`, `*S-1-5-32-545`, …), independiente del idioma del SO.
  - El grant owner-only en directorios no llevaba `(OI)(CI)`, así que los hijos
    (`~/.sky_claw/dlq/`) no heredaban acceso de escritura → el DLQ no podía
    crear/abrir su SQLite. Ahora los directorios se otorgan `(OI)(CI)(F)`
    (heredable); los archivos siguen con `(F)`.
  - La decisión de fail-closed ahora la dicta el **DACL efectivo**
    (`_verify_dacl`), no el exit code de icacls — un `/remove` que falla tras un
    `/grant` exitoso ya no destruye un artefacto correctamente endurecido. La
    garantía owner-only se mantiene (un DACL con ACE no-owner sigue fallando
    closed).
  - **Endurecimiento del review (P1):** la aceptación en el camino *degrade*
    (excepción de icacls) ahora exige el **SID/nombre calificado exacto** del
    owner con Full Control (`(OI)(CI)` en directorios), sin fallback por nombre
    pelado; y los ACE de logon-session sólo se toleran si son de la **sesión
    actual**. La garantía owner-only queda más estricta, nunca más laxa.
- **`py7zr` 0.22.0 → 1.1.3** (CVE-2026-23879). El bump *major* destapó un bug
  latente en `fomod/installer.py::_extract_7z`, que extraía target-por-target en
  un loop — en py7zr ≥ 1.0 eso re-lee el stream y lanza `CrcError`. Ahora extrae
  en una sola pasada (`extractall`) tras validar todos los nombres (anti
  zip-slip), con el test de extracción real multi-archivo que faltaba.

## [0.2.1] - 2026-06-17

### Fixed
- **El ejecutable empaquetado no arrancaba (crash al abrir)** — dos bugs de
  empaquetado:
  - Los assets de la GUI (`styles.css`, `assets/`) no se incluían en el bundle
    de PyInstaller y las rutas no eran *frozen-aware*, así que
    `add_static_files` recibía un directorio inexistente bajo `sys._MEIPASS` y
    el exe crasheaba antes de iniciar (`sky_claw.spec`, `sky_claw_gui.py`).
  - En el build `--windowed`, `sys.stdout`/`sys.stderr` son `None`; el banner de
    arranque de NiceGUI escribía sobre ellos y tiraba el proceso antes de
    bindear el puerto. Nuevo guard `_ensure_std_streams` en `__main__.py`.
- **El agente GUI no bootstrapeaba (`SupervisorAgent.__init__`)**:
  - La resolución de rutas de MO2 validaba contra el `PathValidator` de backups
    (solo `.skyclaw_backups`) en vez del sandbox de modding, rechazando toda
    ruta de MO2 (`RuntimeError` de modlist). Ahora `AppContext` inyecta el
    sandbox validator correcto vía DI (`supervisor.py`, `app_context.py`,
    `_bootloader.py`), sin debilitar el sandbox de rollback.
  - `XEditPipelineService` se construía sin el kwarg requerido `journal`.
  - Nueva cobertura de construcción del `SupervisorAgent` (`__init__` no tenía
    ningún test — el agujero por el que pasaron estos bugs hasta el runtime).
- **`build.bat`** apuntaba a `venv\` en vez del `.venv\` real del repo.

## [0.2.0] - 2026-06-16

### Added
- **OpenAI como proveedor LLM de primera clase** (#185): `OpenAIProvider`
  (OpenAI-compatible, `api.openai.com`), cableado en `create_provider`,
  `--provider`, el wizard GUI, el bridge web y el ops-hub.
- **Modelo LLM provider-scoped** (#186): campos `{provider}_model` en config;
  `app_context` resuelve el modelo del provider activo sin fallback global, así
  cambiar de provider nunca arrastra un modelo incompatible. El `llm_model`
  global legacy se migra al slot del provider activo al cargar.

### Fixed
- **P0 — Procesos externos huérfanos (auditoría de producción)**: los runners
  `bodyslide_runner.py`, `pandora_runner.py` y `wrye_bash_runner.py` capturaban
  `TimeoutError` y retornaban sin matar el proceso del SO, dejando BodySlide /
  Pandora / Wrye Bash vivos reteniendo handles sobre el VFS de MO2 y el
  directorio `Data` de Skyrim. Ahora cada runner hace `kill()` + reap en timeout
  y añade un handler de `asyncio.CancelledError` (mata y re-lanza) para que el
  apagado/cancelación no filtre binarios. Además, `__main__.py` traduce SIGTERM
  a `KeyboardInterrupt` (`_install_sigterm_handler`) para que un `kill <pid>` en
  Unix/WSL2 dispare la misma limpieza grácil que Ctrl+C. Cubierto por
  `test_subprocess_orphan_prevention.py` y `test_graceful_shutdown_signal.py`
  (TDD red→green).
- **P1 — Concurrencia y resiliencia (auditoría de producción)**:
  - **P1-1 `async_registry` race de escritura**: `AsyncModRegistry` comparte una
    única conexión `aiosqlite` entre todas las corrutinas mientras `SyncEngine`
    reparte writes en un `TaskGroup` (hasta 15 tareas). Como la transacción no
    es atómica entre `await`s, el `commit`/`rollback` de un escritor podía caer
    en mitad de la transacción de otro (commit parcial o descarte de filas no
    committeadas = pérdida silenciosa; WAL no protege de la pérdida lógica).
    `upsert_mod`, `set_vfs_status` y los tres writers `executemany` ahora están
    serializados con un `asyncio.Lock`. Test: `test_async_registry_write_serialization.py`.
  - **P1-2 deadlock latente en xEdit/Synthesis**: en timeout hacían
    `proc.kill()` y luego `await proc.communicate()` **sin timeout**; un proceso
    nieto que heredó el pipe lo colgaba para siempre. Ahora reapean con
    `wait_for(proc.wait(), 3.0)` acotado y añaden handler de `CancelledError`.
    Test: `test_runner_communicate_timeout.py`.
- **Audit follow-up (#152, #153, #154, #155)** — cuatro hallazgos de la
  auditoría multidisciplinaria, agrupados en un único bundle TDD:
  - **#153 S-3 — `config.py` nested merge**: `Config._load_from_file`
    extraía `[telegram]/[nexus]/[paths]` a claves flat y luego hacía
    `_data.update(file_data)` con el dict crudo, re-inyectando las
    secciones como dicts paralelos. Ahora se hace `pop()` selectivo
    antes del `update()` para que sobreviva sólo la forma canónica
    flat y la precedencia top-level > nested quede explícita.
  - **#154 PM-2 — scraper doc-drift**: docstrings de
    `scraper_agent.query_nexus` y `scraper/nexus.py` prometían un
    fallback Playwright que en realidad es un stub permanentemente
    deshabilitado por compliance con el ToS de Nexus Mods.
    Reescritos para reflejar la realidad (sólo API oficial).
  - **#155 L-1 — `first_run.py` path validation**: el wizard
    guardaba `mo2_root` / `skyrim_path` sin verificar existencia.
    Nuevo helper `_validate_path` (pure, unit-tested) + wrapper
    `_prompt_for_validated_path` que re-pide al usuario; opción de
    confirmar continuar con una ruta inexistente para instalaciones
    nuevas donde la carpeta se crea después.
  - **#152 A-1 — `executor.py` async resolve**: las dos llamadas a
    `pathlib.Path(...).resolve(strict=False)` del path-jail en
    `ManagedToolExecutor.execute()` ahora pasan por
    `asyncio.to_thread` via el nuevo helper estático
    `_resolve_strict_false`. Reduce el bloqueo del event loop en
    mounts SMB/NFS lentos; test de regresión confirma que la
    rejection de traversals fuera del `modding_root` sigue intacta.

### Changed
- **P1.2 — DLQ obligatoria en producción**: `CoreEventBus.__init__` ahora
  acepta `require_dlq: bool = False` (default mantiene la compatibilidad).
  La factory de producción `create_bus_with_dlq()` lo pasa a `True`, así
  cualquier futuro override que invalida la DLQ (o un constructor manual
  sin DLQ en código de producción) aborta con `ValueError` al construirse
  en lugar de degradarse silenciosamente al modo "drop event on
  backpressure". Tests y dev shells siguen pasando `require_dlq=False`.

### Fixed
- **P1.5 R-06 — Chat preview clear-before-send with rollback**:
  The `_handle_send` closure inside `create_chat_preview` now clears the
  input immediately for a snappy UX. On send failure the original text
  (whitespace preserved) is restored and `ui.notify` surfaces the error.
  Rollback covers both sync exceptions and async failures — when
  `on_send_message` returns an awaitable (the real GUI wires
  `lambda msg: asyncio.create_task(controller.handle_send_message(msg))`),
  a `done_callback` triggers the same rollback path. Logic extracted into
  module-level `_try_send_with_rollback` + `_do_rollback`, unit-tested
  without a NiceGUI runtime (9 cases: sync/async paths, whitespace
  preservation, cancellation handling, best-effort restore/notify).
- **P1 — Reliability bundle (Kimi12 follow-ups)**:
  - **R-05** `SyncEngine._safe_fetch_info` now declares
    `result: dict[str, Any] | None = None` and falls through to an explicit
    `return None`, honoring the declared signature even if `AsyncRetrying`
    ever yields zero attempts (no more silent `UnboundLocalError` risk).
  - **§3.1** `SupervisorAgent` interface TaskGroup split: recoverable
    network errors (`ConnectionError`, `TimeoutError`, `OSError`) are
    logged WARNING and absorbed; everything else is logged CRITICAL and
    re-raised so programming bugs (AttributeError, ValueError, …) stop
    being silently swallowed.
  - **R-07** `SupervisorStateGraph.execute` now auto-purges stale
    `_thread_timestamps` entries: every `_cleanup_interval` (default 100)
    executions it calls `cleanup_old_threads(_cleanup_max_age_seconds)`
    (default 3600 s). Set `_cleanup_interval = 0` to opt out.
  - **R-03** `IdempotencyGuard` keys now carry a TTL
    (`key_ttl_seconds` default 3600 s). A crashed task that never
    `release()`s no longer blocks its key forever — the next acquire
    after the TTL reclaims the slot. Set to `0` for legacy eternal-lock.
  - **§3.2** CLI and GUI chat dispatch are bounded by
    `asyncio.wait_for`: 300 s for `cli_mode._run_cli` / `_run_oneshot`,
    30 s for the new `_dispatch_chat_to_router` helper in
    `gui/_bootloader.py`. A hung LLM provider now surfaces a clean error
    instead of freezing the loop.

### Added
- **P0.5 — Supply-chain reproducibility**:
  - `py7zr>=0.21,<1` declared in `[project.dependencies]` (was a PyInstaller
    hidden import in `sky_claw.spec:39` but missing from the manifest, breaking
    `pip install` reproducibility on fresh envs without 7-Zip support).
  - `requirements.lock` regenerated with `--generate-hashes` (2724 SHA-256
    hashes pinned for integrity verification at install time).
  - `package-lock.json` removed from `.gitignore` and committed for the
    Telegram Node gateway. Builds now reproducible across CI runs.
  - CI Security gate hardened: `pip-audit --strict` (was permissive),
    `npm ci` + `npm audit --audit-level=high` for the Telegram gateway.

### Changed
- **P0.4 — Quality gates**: coverage gate raised from 55 % → 60 % in CI
  (`--cov-fail-under=60`). Actual coverage at gate change: ~65 %. Documentation
  updated in `tests/conftest.py` and `.github/coding_conventions.md`.

## [0.1.0] - 2026-05-11

### Added
- Prometheus observability layer: `Counter` (`sky_claw_sync_attempts_total{status}`),
  `Histogram` (`sky_claw_sync_duration_seconds`), `Gauge` (`sky_claw_queue_depth`,
  `sky_claw_circuit_breaker_state{breaker_name}`). HTTP `/metrics` endpoint on
  `127.0.0.1:9100` with `X-Auth-Token` auth and dedicated `AuthTokenManager` instance
  with rotation.
- Centralized test fixtures in `tests/conftest.py`: `async_registry` (M-01 compliant
  lifecycle), `mock_network_gateway` (async context-manager stub), `correlation_id`
  (ContextVar reset on teardown).
- Cross-platform CI matrix: `ubuntu-latest` + Python 3.12 added to `test` gate;
  Python 3.12 added to `lint` and `typecheck` gates. Total: 10 runs/push (was 5).
  `fail-fast: false` maximises diagnostic signal.
- Dynamic SemVer via `hatch-vcs`: version derived from annotated git tags.
  `release.yml` skeleton for automated GitHub Releases on `v*` push.
- Coverage gate raised from 49 % → 55 % (actual 63.86 %). Policy: +5 pp/sprint
  until 80 % minimum.

### Security
- Harden SQLite pool lifecycle, redaction depth, WS close code and ScraperAgent
  gateway contract ([#120](https://github.com/FacundoSu1986/Sky-Claw/pull/120)).
- Harden PR 118 follow-up gaps ([#119](https://github.com/FacundoSu1986/Sky-Claw/pull/119)).
- Address WebSocket and egress review follow-ups
  ([#117](https://github.com/FacundoSu1986/Sky-Claw/pull/117)).
- Externalize context quarantine and redact modern secrets.
- Harden WebSocket auth and outbound egress.

[Unreleased]: https://github.com/FacundoSu1986/Sky-Claw/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/FacundoSu1986/Sky-Claw/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/FacundoSu1986/Sky-Claw/releases/tag/v0.1.0

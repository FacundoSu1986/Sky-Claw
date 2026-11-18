# R0: re-certificación en rig real de la etapa 9

> **Audiencia:** el operador con el rig Windows, quien aprueba la certificación y el auditor independiente.
>
> **Estado:** runbook **preparado y nunca ejecutado**. Lo escribió quien no tiene el rig, a partir de las fuentes que cita cada
> paquete. Todo paso marcado *(propuesto)* es diseño de medición, no un procedimiento ya corrido: si el rig muestra que no sirve,
> se corrige acá.
>
> **Fuentes canónicas:** la secuencia base de [validación en un rig real](real_rig_validation.md); lo certificado y sus límites en
> el [informe del rig 2026-10-19](../validation/2026-10-19_pr661_unattended_windows_real_rig/final-report.md); las condiciones del
> HOLD en [el fence de supervisión](../audits/2026-11-01_e3r3_job_object_supervision_fence.md) («Cuándo se levanta (y cuándo NO)»)
> y la evidencia previa en [la identidad del Job](../audits/2026-11-03_identidad_del_job_onefile.md) (§11 y §12).
>
> **Última verificación:** 2026-11-16 sobre `origin/main` `0d2a74e0`. Ningún paso se ejecutó contra un rig.

## Para qué sirve (y para qué no)

El PASS del rig del 2026-10-19 certifica el código de ese día, y el propio informe dice que **el rig no se re-ejecutó sobre los
commits posteriores**. Desde entonces entró la pila de contención del broker (#856, #860, #871, #872, #877) y la etapa 9 ganó piezas
que solo se probaron con dobles: el broker que rechaza un bridge de otra versión (#934), el orden de eventos del bridge (#935), el
preflight del backend antes de pedir aprobaciones (#950), la cuarentena operable (`--mode vfs-quarantine`), el progreso de la etapa 9
en `lods_status` y en la GUI (la GUI en #937; el descarte de la muestra al terminar, en #952), el selector de modo de la GUI (#936),
el guard de etapas previas del modo asistido (#906) y la herramienta para medir un build nuevo (#907). **R0 es esa re-certificación**, sobre el `main` vigente y con el `.exe` congelado.

**Lo que R0 no hace:**

- **No levanta el HOLD.** `_CONTENCION_DEL_WORKER_VERIFICADA` sigue en `False` (`sky_claw/local/mo2/brokered_dyndolod.py`). Levantarlo
  es un PR aparte, que cambia solo la constante y sus anclas, con revisión adversarial y **el visto bueno del auditor
  independiente, que lo da con evidencia propia**. R0 produce evidencia para ese PR, no lo sustituye.
- **No certifica el MO2 de producción del operador.** Todo corre sobre una copia aislada.
- **No cambia código.** Se certifica el `origin/main` fijado, sin tocar; si algo falla se registra `FAIL` y se corrige en un PR
  aparte, como hizo el rig anterior con F1 y F2.

## Reglas que no se negocian

Salen del rig anterior (§8-bis y §11 de su informe) y de [la validación base](real_rig_validation.md):

1. **MO2 aislado:** una copia en modo **portable** (`portable.txt`), perfil descartable y canary propio. **Está prohibido** usar o
   modificar la instancia global de MO2 del operador. El único contacto accidental del rig anterior se detectó, se revirtió y quedó
   documentado.
2. **MO2 lo abre el operador, una vez, desde el Explorador** (límite F3 del rig anterior). La única excepción es
   [R0-6](#r0-6-f3-lanzar-mo2-sin-sesión-interactiva), que lanza MO2 desde un proceso no interactivo **solo sobre la copia aislada**
   de la regla 1 para medir justamente ese límite; todo otro paquete abre MO2 desde el Explorador, y la instancia global sigue
   vedada también ahí.
3. **Se certifica código sin cambios.** Nada se arregla durante el rig.
4. **Baseline y backup externos al árbol bajo prueba;** los presets de TexGen/DynDOLOD se restauran byte a byte y se registra su SHA-256
   antes y después.
5. **Evidencia sin secretos ni datos de cuenta:** nada de tokens (Telegram, Nexus), y el nombre de usuario de Windows redactado en las
   rutas (`C:\Users\<usuario>\`).
6. **Un paquete bloqueado no sustituye una corrida verde** ([validación base](real_rig_validation.md), «Evidencia fechada»). Se
   registra `BLOQUEADO` con el motivo, no se omite.
7. **Perfil aislado del proceso: todo proceso de Sky-Claw del rig arranca con `SKYCLAW_ISOLATED=1`** (el arnés, el `.exe` congelado
   y sus workers). Un `HOME` aislado **no** aísla el keyring de Windows, el registro ni las bibliotecas de Steam, que son del
   *usuario*: en el rig anterior un `AppContext` con `HOME` aislado leyó el token de Telegram del operador (más de 700 llamadas de
   long-polling, todas 401) y el autodetect escribió el Skyrim real del operador en la config aislada y lo admitió como raíz del
   sandbox. Con la variable activa el keyring pasa a un backend que **falla** (todo secreto se trata como «no configurado» y no se
   escribe nada) y el descubrimiento de la máquina real se apaga. **Las rutas que el perfil fija en su `config.toml` siguen
   valiendo**, así que el perfil trae `skyrim_path` y demás rutas explícitas. Una corrida que necesite un token real (R0-12) usa un
   **bot de prueba dedicado** con su token en el `config.toml` del perfil, nunca el del operador. Se declara en `isolation.json`.
   Límite conocido: la lectura de solo lectura de `HKCU` que hace la identidad de la instancia de MO2 no se aísla (ver
   `tests/test_aislamiento_perfil.py`, `LECTORES_DEL_REGISTRO`).

## Decisiones previas del aprobador

Hay una restricción de diseño que condiciona el plan, y conviene resolverla **antes** de empezar:

> **Con la barrera puesta, el backend brokered no corre en producción.** El resolver entrega el guard del HOLD, que siempre bloquea;
> solo los tests del cableado levantan la constante (por `monkeypatch`). Así que los paquetes que necesitan una corrida **completa**
> de TexGen/DynDOLOD bajo USVFS requieren una de dos cosas: **(a)** un arnés local que arme la strategy brokered fuera del resolver,
> que **no se commitea** y cuya existencia se declara en la evidencia (el rig del 2026-10-19 también tuvo un arnés, pero para lanzar
> MO2, y la barrera todavía no existía: este sería otro); o **(b)** correr ese paquete en `standalone`, que no depende del HOLD pero
> tampoco prueba USVFS.

Los paquetes marcan su backend. Los que no dependen del HOLD son `vfs-health` (modo de CLI del broker, no pasa por el resolver) y
los que pueden correr en `standalone` sin perder lo que miden. Quien aprueba decide además:

- **N de la serie de [R0-1](#r0-1-contención-con-el-exe-congelado).** El precedente son 24 corridas; un N mayor da más margen.
- **Si una cuarentena cuenta como `FAIL` de R0-1.** Se fija antes de empezar la serie, no después de ver el resultado.
- **Qué arnés** (a) se usa y quién lo revisa.
- **Quién es el auditor independiente** y qué evidencia propia va a pedir (condición 2 del HOLD).

## Mapa de paquetes

| Paquete | Qué prueba | Backend | Necesita | Decide |
|---|---|---|---|---|
| [R0-0](#r0-0-preparación) | Línea base: SHAs, hashes, aislamiento | n/a | nada | todo lo demás |
| [R0-1](#r0-1-contención-con-el-exe-congelado) | Contención del worker con el `.exe` congelado | `vfs-health` | R0-0 | condiciones 1 y 4 del HOLD |
| [R0-2](#r0-2-identidad-del-bundle-del-bridge) | El broker rechaza un bridge de otra versión | `vfs-health` | R0-0 | condición 3 del HOLD |
| [R0-3](#r0-3-preflight-del-backend) | Preflight antes de pedir aprobaciones | brokered (arnés) | R0-0 | **H-6** (auditoría de la etapa 9) |
| [R0-4](#r0-4-f1_rig-texgen-con-el-modal-stitched) | Fix de F1 (`Error:` del modal *stitched*) | standalone | `Data` con output previo | `F1_RIG` |
| [R0-5](#r0-5-f2_rig-dyndolod-con-el-modal-de-dll-ng) | Fix de F2 (reobservar antes de invocar) | standalone | DLL NG **ausente** | `F2_RIG` |
| [R0-6](#r0-6-f3-lanzar-mo2-sin-sesión-interactiva) | Lanzar MO2 sin sesión interactiva, con el preflight de #871 | brokered | R0-0 | F3 |
| [R0-7](#r0-7-s-2-visibilidad-de-archivos-que-no-son-plugins) | Visibilidad de archivos no plugin bajo USVFS | brokered (arnés) | R0-0 | **H-2** residual |
| [R0-8](#r0-8-matriz-de-alias-de-ruta) | Aliases de ruta (8.3, `\\?\`, `subst`, junction, UNC, no ASCII) | standalone | R0-0 | **H-15** |
| [R0-9](#r0-9-smoke-del-alta-de-un-build-p-4) | Herramienta de medición de builds sobre Alpha-209 real | n/a | TexGen y DynDOLOD instalados | **H-8** |
| [R0-10](#r0-10-mediciones-para-diseño) | Worldspaces y prerequisitos (no es pass/fail) | n/a | R0-0 | diseño de **H-9** y **H-10** |
| [R0-11](#r0-11-observación-en-vivo) | Progreso, cuarentena y guard vistos por un operador | cualquiera | una corrida real larga | verificación de la GUI |
| [R0-12](#r0-12-certificación-final) | Telegram real y LOD dinámico | el que se decida | todo lo anterior + DLL NG presente | **H-18** |

Los identificadores `H-n` son los de la auditoría de la etapa 9 (noviembre de 2026); las fuentes que están en el repo se citan en
cada paquete. Orden recomendado: R0-0, R0-1, R0-2, R0-9, R0-4, R0-5, R0-6, R0-8, R0-7, R0-3, R0-10, R0-11 y, al final, R0-12.

## R0-0 Preparación

Se hace una sola vez y su salida alimenta todos los paquetes.

- **`origin/main` y `.exe`.** Fijar el SHA de `origin/main`, construir el `.exe` congelado **desde ese SHA exacto** y registrar su
  SHA-256 **antes** de usarlo. El informe de identidad del Job (§11.5) dejó un `.exe` sin SHA-256 registrado porque se sobrescribió
  antes de hashearlo: es lo que la condición 1 del HOLD pide no repetir.
- **Versiones.** Windows (build), MO2, USVFS, Skyrim, TexGen y DynDOLOD (con sus hashes), Resources SE y DLL NG. Referencia del rig
  anterior: MO2 2.5.2 rev `9c130cbf` / USVFS 0.5.6.1, Windows 10 19045, Skyrim 1.6.1170, Alpha-209 exacto; DLL NG ausente.
- **Token e integridad.** Registrar si el proceso corre con token elevado o filtrado, con una captura (por ejemplo la etiqueta de
  integridad que muestra `whoami /groups`) *(propuesto)*. El informe de identidad (§11.5) dice que el agente anterior informó haber
  corrido con un token filtrado pero no quedó un registro verificable.
- **Aislamiento.** Copia portable de MO2, perfil descartable, rutas con el usuario redactado y `SKYCLAW_ISOLATED=1` en todo
  proceso de Sky-Claw (regla 7). Declararlo en `isolation.json`.
- **Bridge.** Instalarlo con el `.exe` congelado (`--mode install-vfs-bridge`) y registrar el digest del bundle instalado.
- **Estado del HOLD.** Confirmar y registrar que `_CONTENCION_DEL_WORKER_VERIFICADA` es `False` en el SHA certificado.

## R0-1 Contención con el `.exe` congelado

**Fuente:** [identidad del Job](../audits/2026-11-03_identidad_del_job_onefile.md) §11.1 y §11.2; `sky_claw/local/mo2/AGENTS.md`.

Repetir la serie del precedente: **N corridas de `--mode vfs-health`** con el `.exe` congelado y MO2 sandbox abierto desde el
Explorador. El precedente fue de 24 corridas: 23 completas (`rc=0`), 1 en cuarentena, 0 salidas con código 72 y el `hello` en 24/24.

Registrar por corrida: `rc`, si llegó el `hello`, si hubo cuarentena y su causa, si hubo salida con código 72, los tiempos de
`startApplication`, `assign` y `total_to_ack` (en `mo_interface.log`) y la demora entre `job_result` y `worker_exit`.

**Esperado.** Una corrida es verde si termina con `rc=0` y el `hello` llegó. Una salida con código 72 (`EXIT_IDENTIDAD_DEL_JOB` en
`sky_claw/local/mo2/vfs_arranque.py`: el worker no pudo probar su contención y cerró en forma segura) o sin `hello` **es `FAIL` de
R0-1**: cerrar seguro no equivale a haber verificado el Job, que es lo que la condición 1 pide probar. Una cuarentena se registra con
su causa, y quien aprueba fijó antes de la serie si cuenta como `FAIL` (en el precedente hubo una, espuria, con el `hello` en 24/24).

Correr además, en la misma máquina, los tests de laboratorio contra el `.exe` congelado (`SKYCLAW_FROZEN_EXE` apuntando a él):
`tests/test_vfs_job_identity_e2e_windows.py`. Registrar cuántos pasan (el precedente: 6 e2e, 23 de 23 en total con los de laboratorio).

**Qué decide.** La **condición 1** del HOLD (evidencia sobre el binario final con su SHA-256) y la **condición 4** (política de
teardown): el precedente tuvo una cuarentena espuria por una cola lenta de la salida del bootloader (corrida 8: 43,454 s entre
`job_result` y `worker_exit`). Si vuelve a haber una, se registra la causa; no se la reinterpreta.

## R0-2 Identidad del bundle del bridge

**Fuente:** `sky_claw/local/mo2/AGENTS.md` (el `hello` lleva `bundle_digest`); `tests/test_mo2_bridge_identidad_del_bundle.py`.

1. Con el bridge instalado por R0-0, arrancar MO2 y confirmar que el `hello` llega con el digest esperado.
2. *(propuesto)* **Solo en la copia aislada de MO2:** alterar un byte del bundle instalado, reabrir MO2 y confirmar que el broker
   **rechaza** al bridge con la instrucción de reinstalar y reabrir MO2.
3. Reinstalar con `--mode install-vfs-bridge`, reabrir MO2 y confirmar que se acepta.
4. Confirmar que un reinstall **con MO2 abierto** no cambia el digest del proceso en ejecución (el plugin lo calcula una sola vez al
   cargar el módulo).

**Qué decide.** La **condición 3** del HOLD: cómo se actualiza el bundle instalado en MO2.

## R0-3 Preflight del backend

**Fuente:** `tests/test_dyndolod_preflight_del_backend.py`; `sky_claw/local/mo2/AGENTS.md`. **Requiere arnés** (ver las decisiones previas).

Con MO2 **cerrado** o con el bridge no conectado, pedir una corrida desatendida de la etapa 9. Esperado: el operador se entera de que
el backend no está listo **antes** de que se le pida ninguna aprobación, y la corrida no arranca. Repetir con MO2 abierto y el
bridge conectado: el preflight pasa y recién ahí aparecen las aprobaciones. Registrar los mensajes tal como los ve el operador.

**Por dónde entra el arnés (se registra siempre).** La strategy llega al preflight por una sola cadena:
`AppContext.dyndolod_spawn_strategy` (`sky_claw/app_context.py`, donde hoy la asigna el resolver) → `SupervisorAgent`
(`sky_claw/app/gui/_bootloader.py`) → `sky_claw/app/orchestrator/orchestration_composition.py` → el `spawn_strategy` del servicio de
DynDOLOD, que es el que la consulta con `preflight_del_backend` antes de cada pregunta, tanto para la GUI como para el agente remoto.
El arnés tiene que entrar por esa cadena, típicamente asignando `AppContext.dyndolod_spawn_strategy` en lugar de lo que devuelve el
resolver: uno que invoque `preflight_backend` por su cuenta no mide el camino de aprobación. La evidencia registra el archivo y la
función donde el arnés inyecta su strategy y, *(propuesto)*, una traza con marca de tiempo de cada llamada a `preflight_backend`, para
compararla con **cada** aprobación que vio el operador: cada una debe tener una llamada previa a `preflight_backend`, también las
posteriores a la primera (por ejemplo, si el bridge se desconecta entre una y otra). **Sin esos dos registros R0-3 no se marca
`PASS`: queda `BLOQUEADO`.**

## R0-4 `F1_RIG`: TexGen con el modal *stitched*

**Fuente:** informe 2026-10-19 §11 (F1); fila «F1/F2/F3» de `docs/pending_ooda_status.md`; `tests/test_dyndolod_taxonomia_log.py`.

El defecto: el `Error:` del modal `texgen_stitched_lod_previo` se clasificaba terminal y tumbaba una corrida que el operador había
autorizado continuar. El fix está en `main` y su aceptación en rig sigue **pendiente**.

1. Partir de un `Data` con **output previo de TexGen instalado** (la precondición del modal).
2. Correr TexGen y aprobar el modal con `Ignore`.
3. Esperado: éxito por los cuatro conjuntos (código de salida 0, artefacto presente, marcador de completitud, sin terminal), con la
   línea `Error: Found stitched object LOD textures from earlier TexGen generation installed in game folder.` **presente en el log y
   no clasificada como terminal**.
4. Registrar el extracto del log, el veredicto del post-check y que los archivos quedaron en el root administrado.

Los números del rig anterior (1.362 archivos, 138 MB) son contexto y no un umbral de aceptación: dependen del modlist.

## R0-5 `F2_RIG`: DynDOLOD con el modal de DLL NG

**Fuente:** informe 2026-10-19 §11 (F2); fila «F1/F2/F3» de `docs/pending_ooda_status.md`; `tests/test_dyndolod_uia_driver.py`.

El defecto: un hueco transitorio de observación UIA en el paso `INVOCAR` abortaba la corrida sin reintento. El fix (reobservar antes de
invocar y antes de la única escritura del Output) está en `main`; su aceptación en rig sigue **pendiente**.

1. Con **DLL NG ausente** (es lo que dispara el modal `dyndolod_dll_ng_ausente`), correr DynDOLOD y aprobar el modal.
2. Esperado: la corrida atraviesa `INVOCAR` y termina por los cuatro conjuntos.
3. **El hueco transitorio no se puede provocar a demanda.** Registrar si hubo una reobservación (en el log del driver) o, si no la hubo,
   declarar «hueco no observado» en lugar de dar el fix por demostrado: el éxito sin hueco solo prueba que el camino normal sigue
   funcionando.

## R0-6 F3: lanzar MO2 sin sesión interactiva

**Fuente:** informe 2026-10-19 §11 (F3). El preflight de breakaway de #871 es la causa probable según la auditoría.

El rig anterior dio `ERROR_ACCESS_DENIED (0x5)` en `IOrganizer.startApplication` y `failed to spawn` en el log de USVFS **para cualquier
ejecutable**, con MO2 lanzado por el arnés (`subprocess.Popen` desde el proceso de la sesión), y pasó todo con MO2 abierto por el
operador desde el Explorador.

1. Repetir el lanzamiento desde un proceso no interactivo, como el arnés anterior —**solo sobre la copia aislada de MO2**: es la
   excepción de la regla 2 de «Reglas que no se negocian»—, y registrar el resultado de `startApplication` y el log de USVFS.
2. Como control, abrir MO2 desde el Explorador y correr lo mismo.
3. Registrar si el preflight de breakaway de #871 aparece en el log y qué dice.

**Qué decide.** Si el lanzamiento programático ya funciona, se puede discutir prometer un brokered 100 % autónomo; si no, el límite
(«MO2 se abre una vez por el operador») sigue siendo parte del contrato.

## R0-7 S-2: visibilidad de archivos que no son plugins

**Fuente:** `tests/test_dyndolod_solo_brokered_visibilidad.py` (su docstring) y la entrada de CHANGELOG de B-4. **Requiere arnés.**

El hueco que queda tras B-4: la visibilidad de los plugins está anclada, pero **no la de los archivos que no son plugins** (meshes y
texturas). Sky-Claw empaqueta `mods/TexGen Output` por fuera de MO2; si MO2 arma su overlay desde un árbol en memoria sin
refrescarlo, DynDOLOD podría ver un TexGen parcial o viejo y el canary acertar igual. **Es una hipótesis: este paquete la mide.**

*(propuesto)*

1. Con MO2 **ya abierto**, empaquetar un árbol conocido en `mods/TexGen Output` (el flujo normal del producto) y habilitar el mod.
2. Lanzar bajo USVFS un proceso inocuo que **liste** el Data virtual, y comparar su listado con el de disco del mismo árbol:
   archivos que faltan, que sobran y con distinto contenido (hash).
3. Cambiar el contenido de un archivo **después** de que MO2 cargó y repetir.
4. Registrar cuántos ensayos se hicieron y **todas** las discrepancias, sin filtrar.

**Qué decide.** Una sola discrepancia **confirma** H-2 y abre el diseño de un `refresh` en el bridge o de canarios múltiples muestreados
del artifact. Cero discrepancias en los ensayos hechos **cierran la hipótesis solo para esa versión de MO2/USVFS y esos ensayos**, no en
general. El N de ensayos lo fija quien aprueba.

## R0-8 Matriz de alias de ruta

**Fuente:** informe 2026-10-19 §12 («No certifica: aliases de ruta no probados (8.3, `\\?\`, `subst`, junction, UNC)»).

Para cada alias —8.3, prefijo `\\?\`, unidad `subst`, junction, UNC y rutas con caracteres no ASCII— configurar la ruta (Data, root de
salida, `-t:`) y observar dos cosas por separado: si el eco `Using Output Path:` de cada binario coincide con lo configurado y si los
archivos esperados caen en el root donde Sky-Claw los busca **y los escribió esta corrida**. Un eco que coincide **no** prueba que los
archivos estén: se miran los dos.

**Frescura.** Un archivo que ya estaba de una corrida anterior no cuenta: sin esto, un alias defectuoso que escribe en otro destino
pasaría por `PASS` porque el root conserva lo de la fila anterior. Antes de cada corrida se vacía el root de salida **del perfil
descartable** (con su backup, regla 4), que es lo preferible; la alternativa es exigir que los archivos tengan una marca de tiempo
posterior al inicio de la corrida. Abajo, «archivos esperados» significa los esperados **de esta corrida**.

Resultados válidos por fila:

| Resultado | Significado |
|---|---|
| `PASS` | El eco coincide **y** los archivos esperados están en el root **y los escribió esta corrida** (ver «Frescura»). |
| `FALLA CERRADA` | El eco no coincide **o** faltan archivos esperados, y la corrida **falla** por evidencia de rutas o por salida ausente. Es un resultado aceptable: no hay falso verde. |
| **`FALSO VERDE`** | El eco no coincide **o** faltan archivos esperados en el root, y la corrida **termina en verde**. Es el único resultado inaceptable y se reporta como defecto. |

TexGen y DynDOLOD se miden **por separado**: son binarios distintos y uno que pasa no dice nada del otro.

## R0-9 Smoke del alta de un build (P-4)

**Fuente:** [alta de un build nuevo](dyndolod_alta_de_build_uia.md), «Lo que no está medido»; `scripts/dyndolod_medir_contrato_uia.py`.

La herramienta de solo lectura que mide un build y lo diagnostica contra el contrato congelado se probó **solo con una GUI simulada**.
El primer paso del rig es correrla contra el Alpha-209 real, TexGen y DynDOLOD por separado, y comprobar que cada pieza del contrato da
`COINCIDE` (no `DIFIERE` ni `NO_OBSERVADO`: sin las instantáneas del diálogo terminal y de los modales el veredicto es `incompleto`).
Seguir el procedimiento de ese documento y adjuntar el informe que emite.

## R0-10 Mediciones para diseño

No son pass/fail: alimentan decisiones de diseño y se registran como hechos.

- **Worldspaces (H-9).** Hoy no existe una verificación de worldspace: se aprueba la huella del preset, y el operador elige el
  worldspace con botones de la GUI. Medir los controles y las claves del preset que determinan la selección.
- **Prerequisitos (H-10).** Dónde están Resources SE y DLL NG según el ganador del overlay, y por qué mecanismo se detectan hoy (por
  el modal o por el log).

## R0-11 Observación en vivo

Durante cualquier corrida real larga, un operador mirando la GUI y el agente. Son cosas que los tests de la etapa 9 declaran **no
verificadas**:

- `lods_status` y el panel de la GUI muestran el progreso con la **edad de la muestra** a la vista, y la muestra **desaparece** al
  terminar la corrida, también si termina por error o por cancelación.
- La cuarentena del broker se ve en la GUI y en `lods_status`, y `--mode vfs-quarantine` informa y libera con evidencia.
- El modal del guard de etapas previas del modo asistido (categoría `dyndolod_etapas_previas_asistido`) se abre **en la GUI**, no por
  Telegram, cuando faltan las etapas 6–8.

## R0-12 Certificación final

Solo cuando lo anterior está registrado. Cubre lo que el informe de 2026-10-19 declara **no certificado**:

- **Telegram como canal real** (el rig anterior dejó `telegram_verificado=false`), con un bot configurado.
- **LOD dinámico generado,** con DLL NG **presente** (el rig anterior no pudo: estaba ausente).

El backend lo decide quien aprueba (ver las decisiones previas). El resultado se declara con sus límites: nada de eso certifica
producción.

## Plantilla de evidencia

Un paquete por fecha en `docs/validation/<fecha>_r0_recertificacion/`, con la estructura del paquete del 2026-10-19 como precedente
(no como obligación): `final-report.md`, `environment.json`, `isolation.json`, `binary-hashes.json` y los extractos de log y salidas
de cada paquete.

```markdown
# R0: re-certificación en rig real, <fecha>

> **Operador:** <nombre o rol>  ·  **Aprobador:** <rol>  ·  **Auditor independiente:** <rol>

## Línea base

| Dato | Valor |
|---|---|
| `origin/main` | <SHA de 40 caracteres> |
| `.exe` congelado | <nombre> · SHA-256 <hash> (registrado **antes** de usarlo) |
| `_CONTENCION_DEL_WORKER_VERIFICADA` | `False` (confirmado en el SHA) |
| Windows / MO2 / USVFS / Skyrim | <versiones> |
| TexGen / DynDOLOD / Resources SE / DLL NG | <versiones y hashes; «ausente» si falta> |
| Token e integridad | <elevado o filtrado, con la captura> |
| Aislamiento | MO2 portable, perfil <nombre>, `SKYCLAW_ISOLATED=1`; **no** se tocó la instancia global ni el keyring del operador |
| Arnés brokered | <ninguno / descripción, revisor y punto de inyección (archivo y función) de la strategy; no se commitea> |

## Resultados

| Paquete | Escenario | Esperado | Observado | Resultado | Evidencia |
|---|---|---|---|---|---|
| R0-1 | N corridas de `vfs-health` | … | … | PASS / FAIL / BLOQUEADO / NO EJERCIDO | `<archivo>` |

## Desvíos y hallazgos

<todo lo que no coincidió con lo esperado, **sin filtrar**; cada defecto con su PR aparte propuesto>

## Qué NO certifica (obligatorio)

<lo que quedó fuera de esta corrida y por qué, en la misma forma que §12 del informe de 2026-10-19>

## Firmas

Operador: <…>  ·  Aprobador: <…>  ·  Auditor independiente: <…, con su evidencia propia>
```

Vocabulario de resultados: `PASS`, `FAIL`, `BLOQUEADO` (con el motivo del entorno) y `NO EJERCIDO`. Un `NO EJERCIDO` es evidencia de
precondiciones incompletas y no una validación.

## Después de la corrida

1. Subir el paquete de evidencia en un PR **de documentación** y agregarlo al índice de [evidencia de validación](../validation/README.md).
2. En **el mismo PR**, actualizar las filas de `docs/pending_ooda_status.md` que cambian de estado (`F1_RIG`, `F2_RIG`, F3 y las que
   R0 cierre o reabra). El título del PR declarando «cerrado» no alcanza.
3. Cada `FAIL` abre un PR correctivo aparte, con su test en rojo. **Nada se corrige dentro del PR de evidencia.**
4. Recién entonces, y como PR aparte, se puede discutir levantar el HOLD, con el visto bueno del auditor independiente.

## Lo que R0 no cubre

- **Una prueba con un proceso real que se niega a morir** (el doble fallo de §6 de la identidad del Job, listado en §11.5 como no
  verificado). No hay un procedimiento seguro definido y no se improvisa en el rig.
- **El MO2 de producción del operador** y el comportamiento **bajo carga** (§11.5 de la identidad del Job).
- **Otras versiones** de MO2, USVFS, TexGen o DynDOLOD, y rigs VR (para un build nuevo, ver [R0-9](#r0-9-smoke-del-alta-de-un-build-p-4)).
- **Las etapas 6–8** (Wrye Bash, Synthesis, grass) como ejecución real: el guard verifica sus artefactos, no las corre.

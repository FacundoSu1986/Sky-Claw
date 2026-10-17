# PR #739 / #592 — Rig real Data Path (Findings 4/5)

> **Sesión:** `DYNDOLOD-739-REAL-RIG-DATA-PATH` · **Fecha:** 2026-10-17 · **Rama:** `fix/dyndolod-592-f4-f5-output-evidence`
> **Worktree:** `E:\Skyclaw_Main_Sync\.worktrees\dyndolod-592-f4-f5-output-evidence`
> **Veredicto:** `DYNDOLOD_739_REAL_RIG_PASS_SCOPED` para el contrato Data Path en el modo medido.
> **NO es un PASS universal ni un merge.** No se mergeó #739. No se cerró #592. No se iniciaron Findings 8/9.

## A. Identidad

| Elemento | Valor |
|---|---|
| HEAD del PR al iniciar | `761527cd7d89303f1237f9d0386abb8376d6c568` |
| `origin/main` al iniciar | `d65baa1cdf9814d41b20430d71a89e4a5c9e8a00` |
| `origin/main` al cierre | `1ddc8c8e0373c1366cea9fd3eaca701a522187ae` |
| Base declarada del PR | `9fe88cb48ddbe01823da86da9eedc5dad7f260cd` |
| TexGenx64.exe | 35.794.432 B · v3.0.0.209 · SHA256 `0939BC8F8CBAE2E1B38F17D56FECD1B7A941DADE0554E944886BD7F5C4807A62` |
| DynDOLODx64.exe | 35.794.432 B · v3.0.0.209 · SHA256 `B67625EB7815111BA9AC232C626FF88B07BB64FF30FF5183C5A04B5F045C3BD0` |
| Archivo Alpha-209 | SHA256 `D59B09BD3671A1E825B81A14A97DDBEF160B83AEFAAEBD7B535F78BF67EEC736` |
| SkyrimSE.exe del rig | v1.6.1170.0 · SHA256 `C434208894F07F604B852F29B8EDC3A58C4DE63DE783373733E72B2B73F33BE9` |

## B. Aislamiento

Rig exclusivo: `C:\SkyClaw_Rigs\PR739 Data Path`. Binding
`12f60f92-e0b5-4b10-afc2-8809c659cb71`, estado `C_BINDING_COMPATIBLE`, work root
`...\Work Root`. Perfil de prueba `DataPathRig`; instancia MO2 de prueba
`...\mo2-instance`; mods `...\mo2-instance\mods`.

- Data aislado: `...\game\Data`, 22.556 archivos / 18.329.994.075 B, construido
  desde `G:\Modding\Skyrim_Stock_1.6.1170\Data` (16.133 / 18.021.362.101 B) +
  `DynDOLOD Resources SE` (6.085 / 264.014.154 B).
- INIs: copias exactas de `SkyClaw-PR2-Rig` (SHA idénticos a origen).
- Plugins de prueba: 5 masters, 0 light. Preflight `status=green`,
  `blocks_mutations=False`; `vfs`, `masters`, `plugin_limits`, `master_order`,
  `overwrite`, `write_permissions`, `vfs_visibility` verdes.
- Ningún junction/symlink en el rig ni en las raíces activas escaneadas.
- **Escrituras de terceros observadas**: `profiles` (8 archivos, 16:35–16:40),
  `overwrite` (5, 16:36–16:39) y `mods` (2 `.pex`, 16:27:36–38). Son de otro
  proceso/sesión (MO2/juego/otro agente); esta sesión no las produjo y sus
  comandos no contienen rutas activas. Las fuentes usadas (Stock Data, INIs,
  Resources) no cambiaron en la ventana. Detalle completo local en
  `evidence\active-protected-scan.json`; nombres activos omitidos acá.
- **Límite**: no se puede afirmar inmutabilidad global de la máquina; sí se
  afirma que el rig no escribió en las rutas activas.

## C. TexGen real

Comando efectivo (argv del servicio; el backslash final duplicado es el esperado
por `list2cmdline` al citar rutas con espacios):

```text
"...\TexGenx64.exe" -sse "-o:C:\SkyClaw_Rigs\PR739 Data Path\Work Root\DynDOLOD\TexGen\\"
  "-d:C:\SkyClaw_Rigs\PR739 Data Path\game\Data\\"
  "-m:C:\SkyClaw_Rigs\PR739 Data Path\Game INI\\"
  "-p:C:\SkyClaw_Rigs\PR739 Data Path\mo2-instance\profiles\DataPathRig\plugins.txt"
  "-t:C:\SkyClaw_Rigs\PR739 Data Path\temp\\"
```

Log de ESTA corrida (`Logs\TexGen_SSE_log.txt`, 111.715 B, 623 líneas):

```text
Using Skyrim Special Edition Data Path: C:\SkyClaw_Rigs\PR739 Data Path\game\Data\
Using Output Path: C:\SkyClaw_Rigs\PR739 Data Path\Work Root\DynDOLOD\TexGen\
[01:29] TexGen completed successfully
[01:29] Exit TexGen, zip and exit, check log or restart?
```

| Clave | Declarada | Configurada | `evaluar_evidencia_de_rutas` |
|---|---|---|---|
| data | `...\game\Data\` | `...\game\Data` | **COINCIDE** |
| output | `...\Work Root\DynDOLOD\TexGen\` | `...\Work Root\DynDOLOD\TexGen` | **COINCIDE** |

Terminación: `Exit TexGen` (NO `Zip and Exit`), exit code 0. Artefacto: 1.362
archivos de texturas + `meta.ini`; digest de handoff
`8fa39ec3c3091bcc0a10260642a32d39f3f1eb79d1a6fdeb75a5350817b563c2`, 1.362
archivos / 138.457.820 B. Error no fatal del log: `<Error: File not found
...\game\SkyrimSE.exe>` (el rig aún no tenía el exe; se copió después para
DynDOLOD). No impidió la completitud.

**Corte esperado:** el servicio devolvió `success=false, needs_deployment=true`
porque el standalone no hereda la vista USVFS del mod `TexGen Output`. Es el
contrato documentado, no un fallo del tool.

## D. Handoff

1. Estado durable creado: `handoff_id=1`, `awaiting_deployment`, perfil
   `DataPathRig`, digest/archivos/bytes esperados.
2. Materialización: `TexGen Output\textures` → `game\Data\textures` con
   robocopy (`rc=3`: archivos copiados + extras; 1.362/1.362 archivos, 0
   errores).
3. Validador real: **`RESULTADO: VISIBILIDAD_OK`**.
4. Journal final: `deployment_handoffs.state=completed` (19:58:53Z).
5. Gap de evidencia durable: `observed_digest`, `observed_files` y
   `observed_bytes` quedaron `NULL` en la DB. El validador externo pasó; el
   servicio no persistió los observados. No se reescribió la DB.

## E. DynDOLOD real

Comando efectivo:

```text
"...\DynDOLODx64.exe" -sse "-o:C:\SkyClaw_Rigs\PR739 Data Path\Work Root\DynDOLOD\DynDOLOD\\"
  "-d:C:\SkyClaw_Rigs\PR739 Data Path\game\Data\\"
  "-m:C:\SkyClaw_Rigs\PR739 Data Path\Game INI\\"
  "-p:C:\SkyClaw_Rigs\PR739 Data Path\mo2-instance\profiles\DataPathRig\plugins.txt"
  "-t:C:\SkyClaw_Rigs\PR739 Data Path\temp\\"
```

Log de ESTA corrida (`Logs\DynDOLOD_SSE_log.txt`, 959.656 B, 10.953 líneas):

```text
Using Skyrim Special Edition Data Path: C:\SkyClaw_Rigs\PR739 Data Path\game\Data\
Using Output Path: C:\SkyClaw_Rigs\PR739 Data Path\Work Root\DynDOLOD\DynDOLOD\
[03:46] DynDOLOD plugins generated successfully
[03:46] Occlusion.esp completed successfully
[03:50] User says "Save and Exit"
Saving ...\DynDOLOD.esm / .esp / Occlusion.esp
```

| Clave | Declarada | Configurada | `evaluar_evidencia_de_rutas` |
|---|---|---|---|
| data | `...\game\Data\` | `...\game\Data` | **COINCIDE** |
| output | `...\Work Root\DynDOLOD\DynDOLOD\` | `...\Work Root\DynDOLOD\DynDOLOD` | **COINCIDE** |

Servicio: **`success=True`, `needs_deployment=False`, `assisted=True`,
duración 348,6 s; `errors=[]`**. Tool: `success=True`, `return_code=0`,
`output_path=...\Work Root\DynDOLOD\DynDOLOD`, 137 warnings no terminales.

Artefactos empaquetados en `...\mo2-instance\mods\DynDOLOD Output`:

| Archivo | Bytes | SHA256 |
|---|---:|---|
| `DynDOLOD.esm` | 193.555 | `EA95B05266A840A706D8AB50F34C02342F928B486D8DE92CF6E00302006312AB` |
| `DynDOLOD.esp` | 307.742 | `34377E40716578A78BE8CED60A26EBF9035E57D9551B1B0189C72B832DCD1870` |
| `Occlusion.esp` | 9.368.071 | `4C9B5C66CF730B353A509B978B11F41B2A845229B885FE7F42D60F8D80E2EA8F` |

Paquete: 5.121 archivos / 897.631.366 B.

### HITL / GUI — condición real

- `START_HABILITADO` se emitió **antes** de la generación (final gate MATCH).
- El driver no encontró el botón Start (`BOTON_INICIO_AMBIGUO`, `candidates=[]`);
  el **operador** pulsó Start después de la habilitación y pulsó `Save and Exit`.
  El Ignore del modal DLL también lo pulsó el operador (el driver lo registró
  como `modal_benigno_espera_humano`). No hubo auto-Start ni click del driver
  sobre Start/Save.
- Esto es una limitación del driver de la sesión, no del gate: el gate observó
  MATCH y autorizó; el humano ejecutó la acción. El servicio reportó
  `assisted=True`.

### Warnings/errores no terminales

`Error: DynDOLOD.DLL ... not found!` → **dynamic LOD no generado**; el output es
válido para object/static LOD, no para dynamic LOD. Además: 117 líneas
`<Error: ...>` no fatales (deleted references de Update/Dawnguard/HearthFires,
Unresolved FormID en Dawnguard.esm), `Can't find Skyrim - Patch.bsa`, `Could not
find save path`, grupos vacíos de xEdit. Los marcadores de completitud y el
return code 0 se cumplieron; no se hallaron errores terminales atribuibles.

## F. Procedencia — representaciones y riesgos

Medido con binarios reales:

- El argv lleva `Data\\` / `DynDOLOD\\` (backslash final duplicado por el
  quoting de rutas con espacios); el binario ecoa `Data\` / `DynDOLOD\`.
  `rutas_equivalentes` (ntpath) los declara equivalentes.
- Alias probados en la matriz negativa (sin tocar binarios): mayúsculas,
  backslash duplicado, barra final → **COINCIDE**.
- Ruta con espacios: medida en `-o:`, `-d:`, `-m:`, `-p:`, `-t:`; funciona.
- **NO_TESTED** (no se forzaron en el rig para no cruzar fronteras): 8.3,
  `\\?\`, `subst`, junctions, UNC, semántica MO2/USVFS. No se extrapola.
- Un MATCH textual acredita el parse del argv, no la visibilidad de toda la
  modlist. El handoff standalone se validó con el validador real de visibilidad;
  MO2/USVFS no se reprodujo.

Matriz negativa (10 casos, `evidence/negative-matrix.json`): Data desviado →
`DIFIERE` y `datos_desviados=true` (fail-closed); Data ausente → `AUSENTE`;
Data indeterminado → `INDETERMINADO`; log truncado → `AUSENTE`; Output
desviado con artefacto → `DIFIERE` advisory (`salida_desviada=true`), Data
correcto. Casos 11/12 (TexGen fallido no arranca DynDOLOD; cancelación no
publica éxito) están en `tests/test_dyndolod_service.py` de la suite focalizada.

**Alcance de los casos 2–4 (`EVALUATOR_ONLY`).** Registran rutas de entrada y el
veredicto de `evaluar_evidencia_de_rutas`, nada más: no registran presencia de
artefacto, resultado del runner ni empaquetado, y no son una corrida negativa real.
El rechazo con artefacto presente y la ausencia de empaquetado los prueban, con
ejecución **simulada** (`_execute_process` parcheado), los tests que cada caso cita
en `evidencia_simulada_del_runner`:
`test_p1_data_desviada_con_artefacto_presente_no_es_exito[TexGen|DynDOLOD]` y
`test_p1_data_desviada_no_empaqueta_el_mod`.

## G. Integridad

- Roots de salida exclusivos; TexGen y DynDOLOD no mezclan directorios.
- No se borraron outputs previos de producción; los outputs de intentos
  fallidos del rig fueron descartados por el propio servicio (rollback).
- Journal final: **TX1 committed** (TexGen/handoff), **TX2/TX3/TX4 pending**
  (intentos DynDOLOD fallidos), **TX5 committed** (DynDOLOD final). No se
  ejecutó recuperación ni limpieza de las pendientes; quedan registradas.
- Handoff final `completed`, con los observados `NULL` (ver D).
- Sin procesos TexGen/DynDOLOD/LODGen residuales al cierre.
- No se publicaron binarios de Bethesda, assets privados ni datasets de mods.
- El rig se conserva completo para revisión; no se borró ni se restauró nada.

## H. Validación

- `ruff check` / `ruff format --check` / `mypy` / `git diff --check`: **verdes**.
- Suite focalizada `test_dyndolod*.py`: **1206 passed, 2 failed, 10 skipped**.
  - `test_el_heartbeat_mantiene_viva_la_lease_de_ownership`: falló bajo carga,
    pasó aislado (flaky).
  - `test_la_cancelacion_durante_la_resolucion_libera_la_lease`: falla
    consistente en aislamiento (lease de ownership sobrevive a la cancelación).
    No se tocó el test ni el código; queda para Tech Lead.
- Qodo Regression & Test Oracle: **FAILURE** (run 38963880570, attempt 2) por
  `litellm.Timeout`/OpenRouter y respuesta sin mapping de review — infraestructura,
  no pytest. No se califica como SUCCESS. Qodo Auto-revisión: SUCCESS.
  Resto de CI observado: SUCCESS en el head 761527cd.
- `git merge-tree` contra `origin/main` actual (`1ddc8c8e`): **sin conflictos**
  (tree `c88dc1b817d045ae5b385c3ef435db1b79f83abb`). La rama está behind 18;
  no se hizo merge/rebase ni test-merge con checkout.
- Thread P1 `PRRT_kwDOR1JjU86s_sXd` (Data root fail-closed): **ABIERTO**; no se
  resolvió. Los demás hilos están resueltos/outdated.

## I. Decisión

**`DYNDOLOD_739_REAL_RIG_PASS_SCOPED`**

Alcance certificado:

- Alpha-209 exacto (hashes arriba), Windows 10 19045, rig aislado.
- Modo **standalone** (sin USVFS): `MO2_USVFS_NOT_VERIFIED`.
- Perfil de prueba `DataPathRig`, Data Stock 1.6.1170 + DynDOLOD Resources SE.
- TexGen real: eco Data/Output **COINCIDE**, exit 0, `Exit TexGen`, handoff
  materializado y `VISIBILIDAD_OK`.
- DynDOLOD real: eco Data/Output **COINCIDE**, exit 0, `Save and Exit`, servicio
  `success=True`, artefactos empaquetados; Start/Save operados por el humano
  después de `START_HABILITADO`.
- Dynamic LOD **no** certificado (DLL NG ausente); el output es object/static.

**No certifica:** MO2/USVFS, otras versiones/configuraciones, aliases no
probados (8.3, `\\?\`, subst, junctions, UNC), inmutabilidad global de la
máquina (hubo escrituras de terceros en la instancia activa), gates de calidad
verdes (falla el test de cancelación de lease), ni ausencia de TX pendientes.

**Abierto para Tech Lead:** decidir si las TX2–TX4 pendientes, el fallo del test
de workspace y el fallo de infraestructura de Qodo impiden el merge. Esta sesión
no mergea, no cierra #592, no resuelve el thread P1 y no inicia Findings 8/9.

## J. Adjudicación del Tech Lead (2026-10-17, posterior al rig)

Cada punto dice cómo se verificó. Nada de esto se ejecutó sobre el rig ni sobre
su journal: vive en la máquina del operador y esta adjudicación no lo tocó.

**TX2/TX3/TX4 `pending` — evidencia fail-closed correcta, no mutaciones huérfanas.**
*Verificado leyendo el código; el estado del journal del rig NO se inspeccionó.*

- Por qué quedan `pending`: `DynDOLODPipelineService` fija
  `mutation_coverage_complete = False` en toda corrida (comentario "Cobertura
  honesta": el staging crudo del binario y su temp no están bajo move-aside), y
  `_cerrar_tx_tras_rollback` sólo marca `ROLLED_BACK` si la cobertura es completa
  **o** el runner nunca arrancó. Toda corrida fallida DESPUÉS del spawn termina
  `pending` por diseño: el journal no afirma un rollback total que no puede
  probar. Es comportamiento de `main` anterior a este PR; el diff de #739 no toca
  `dyndolod_service.py` (salvo tests) ni el journal.
- Qué quedó en disco: según `attempts-summary.txt`, el servicio revirtió los
  outputs administrados de los tres intentos. Lo no cubierto es el directorio del
  ejecutable y el `-t:` del rig, ambos dentro de `C:\SkyClaw_Rigs\PR739 Data
  Path` (`dyndolod-command.txt`). Ninguna ruta de producción está nombrada.
- Qué hace el sistema con ellas sin intervención: `OperationJournal.open()`
  llama a `sweep_stale_pending()`, que pasa a `ROLLED_BACK` las `pending` de más
  de 24 h y escribe en el MISMO boundary un receipt durable
  (`stale_pending_sweep_receipts`). Sólo cambia el estado de la fila; no toca el
  filesystem. Mientras siguen `pending`, sólo cuentan como candidatas orphan
  (`_candidatas_orphan_en_conn`) para artefactos que su manifest nombra, es
  decir, rutas del rig.
- **Disposición:** conservarlas. Sin SQL manual, sin transiciones forzadas, sin
  limpieza. Si el operador quiere confirmar el estado antes del barrido, la
  consulta read-only es
  `SELECT transaction_id, status, created_at FROM transactions WHERE status = 'pending';`
  sobre el journal del rig (las etiquetas TX2–TX4 de este informe se asumen como
  esos `transaction_id`; la consulta por estado no depende de esa suposición). Si el rig se descarta, se descarta entero (namespace +
  journal), decisión del operador.

**`test_la_cancelacion_durante_la_resolucion_libera_la_lease` — defecto previo y
real, ajeno a este PR.** *Reproducido ejecutando en Linux; causa leída en el código.*

- En `SnapshotTransactionLock.__aenter__` (`sky_claw/app/db/locks.py`), el paso 1
  (`acquire_lock`) está FUERA del `try/except BaseException` que libera el lock
  si la entrada se corta. Una cancelación que llega después de que la fila quedó
  persistida y antes de que el `await` retorne deja la lease de ownership viva
  hasta su TTL. En una máquina lenta esa ventana cae dentro de los 0,15 s del test.
- Reproducción: el mismo test con `acquire_lock` demorado 0,5 s DESPUÉS de
  persistir → `AssertionError: la cancelación dejó la lease viva`. Sin demora,
  pasa. En CI (Windows py3.11/py3.12) pasa sobre el head de este PR.
- #739 no toca `locks.py`, `dyndolod_workspace.py` ni ese test. No bloquea el
  merge; se arregla en un PR propio, sin debilitar el test.

**Qodo (`Auto-revisión`, `Regression & Test Oracle`).** Las dos ejecuciones
fallidas sobre `cfb970e2` murieron en el proveedor de modelos
(`Service temporarily overloaded` / respuesta sin mapping de review), no en
pytest. No se califican como verdes: se re-ejecutan sobre el head integrado con
`main`.

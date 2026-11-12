# Auditoría de alcance y compatibilidad — cableado HITL de la transición Golden Master

- **Fecha:** 2026-11-11
- **PR:** P7-A5 PR 3 (`arena/bb4f2e9a-sky-claw`, base `8f30c45` = #899 mergeado)
- **Alcance:** `EMISION_PRODUCTIVA_HABILITADA = False` durante toda la exploración; sólo
  fixtures y rigs sintéticos; **no** se tocó MO2 real, Steam, Stock, Generation, Clone
  ni configuración productiva; **no** se ejecutó la primera transición real; **no** se
  abrió la emisión productiva; **no** se inició P7-A6.
- **Veredicto:** el cableado se puede implementar **sin relajar ningún contrato
  existente**, con una única pieza nueva (el consumidor autorizado) y tres
  correcciones dentro del propio cableado. La emisión productiva sigue cerrada y
  **no** se propone abrirla en este PR.

## 0. Método

1. Reconstruir el flujo real leyendo el código, no los documentos.
2. Enumerar por AST los llamadores productivos de cada capacidad (mismo instrumento
   que usan los censos de P4-S6: `tests/test_frozen_runtime_p4_s6_owner_hitl.py`).
3. Clasificar cada punto desconectado y probar la ausencia de bypass con un test que
   **falle** si el bypass reaparece.
4. Recién después, decidir la implementación mínima.

## 1. Flujo real reconstruido (antes de este PR)

| # | Paso del flujo | Implementación existente | Llamador productivo |
|---|---|---|---|
| 1 | Solicitud (propuesta) | `approval.proponer_aprobacion` (`approval.py:2050`) | **ninguno** |
| 2 | Presentación al propietario | `owner_consent.RegistroConsentimientos.presentar` (`owner_consent.py:591`) | **ninguno** |
| 3 | Decisión | `...responder` (`owner_consent.py:642`) | **ninguno** |
| 4 | Emisión | `owner_consent.emitir_aprobacion_autorizada` (`owner_consent.py:780`) | **ninguno** (y falla cerrado por `approval.py:158`) |
| 5 | Consumo | `approval.consumir_aprobacion` (`approval.py:2575`) | el binder (`runtime_clone_binder.py`) |
| 6 | Binder | `runtime_clone_binder.ejecutar_transicion_binder` (`runtime_clone_binder.py:618`) | **ninguno** |
| 7 | Verificación | POST VERIFY interno del binder + `approval.verificar_consumo_aprobacion` (`approval.py:2894`) | el journal (creación del PENDING) |
| 8 | Finalización | `journal._finalizar_locked` | el binder |

Conclusión del paso 1: **los pasos 1–4 y 6 no tenían llamador productivo**. La
maquinaria estaba completa y probada pieza por pieza, y la cadena no existía.

## 2. Puntos productivos desconectados

| ID | Punto | Estado previo | Riesgo si se abría la emisión sin cablear |
|---|---|---|---|
| D-01 | Propuesta: nadie arma el `ApprovalScope` desde evidencia verificada | AUSENTE | el scope sería un conjunto de strings del llamador (el defecto que P4-S5 cerró para el journal) |
| D-02 | Presentación/decisión: `presentar`/`responder` sin superficie | AUSENTE | `HITL_PRODUCTIVE = NOT_WIRED` |
| D-03 | Emisión: interruptor cerrado | CERRADO | `ISSUED` sin salida operativa y `MAX_ISSUED = 1` bloqueando la siguiente |
| D-04 | Ejecución: binder sin emisor | AUSENTE | única vía: fixtures de tres cadenas (no es consentimiento humano) |
| D-05 | Re-verificación entre presentación y ejecución | AUSENTE | el dueño aprobaría un estado del mundo que puede haber cambiado |
| D-06 | Una transición a la vez (más allá de `MAX_ISSUED = 1`) | AUSENTE | segunda aprobación sobre una aprobación ya consumida = autorizar dos veces lo mismo |
| D-07 | Reanudación de una aprobación consumida | PARCIAL (el binder acepta `aprobacion_consumida`) | nadie reconstruía la evidencia de consumo desde la autoridad durable |
| D-08 | `candidate_id` en el camino Stock→Generation | AUSENTE | `PROMOTION` exige el campo (§38.5 campo 10) y el provisionamiento no crea Candidates |
| D-09 | Evidencia de contenido del Clone | PARCIAL | ver **F-03** |

## 3. Verificación de identidades (lo que el cableado exige y de dónde sale)

| Identidad | Verificación | Dónde |
|---|---|---|
| **Root** | `root_id` lo acuña `presentar` y lo revalida la puerta durable al emitir; el consumo cruza contra el witness | `owner_consent.py:591`, `approval._exigir_binding_consistente` |
| **Generation** | `verificar_generation == VALID`, metadata presente, `generation_id` canónico | `generations.verificar_generation` |
| **Clone** | registro `PROVISIONED`, `verified == intended` setup, `verificar_runtime_clone == VALID`, **TreeDigest observado == Generation** (F-03) | `clones.verificar_runtime_clone`, `autorizacion_hitl_transicion._` |
| **Instancia MO2** | `Mo2InstanceIdentityState.ACTIVE_GLOBAL` (HKCU `CurrentInstance` + sin artefactos portables) y `approval_provenance` liga la instancia + SHA-256 del INI (F-05) | `binder_preflight` (`MO2_INSTANCE_IDENTITY`) |
| **Procedencia Stock A1** | bajo `STOCK_INDEPENDENT_TRANSITION`: preflight `READY` con digest externo aprobado; el cableado re-exige el digest después de `READY` | `provisioning.cargar_digest_aprobado_stock`, `binder_preflight` |
| **Transición autorizada** | `approval_scope_digest` recomputado en 4 puntos (proponer, presentar, emitir, consumir); proyección witness ↔ propuesta en el consumo y en el `CREATE` del journal | `approval`, `journal` |

## 4. No existe bypass del consentimiento — análisis adversarial

Cada fila tiene un test que la hace fallar si el bypass reaparece.

| Vector | Por qué no funciona | Test |
|---|---|---|
| B-01 Consentimiento fabricado (tres cadenas) para consumir sin emisión | `consumir_aprobacion` exige `ISSUED` con el digest en el ledger y en el witness; sin emisión no hay nada que consumir | `test_frozen_runtime_p4_s4_approval_authority`, `test_01/02` de P5-C1 |
| B-02 Emitir sin identidad verificada | `_exigir_binding_completo` + `_exigir_binding_consistente` (witness v3 con `solicitud_binding` acuñado por `responder`) | `test_frozen_runtime_p4_s7_witness_request_binding` |
| B-03 Segunda superficie de consentimiento | el cableado no construye `ConsentimientoAprobacion` ni usa `_emitir_aprobacion_bajo_lock` (censo por texto sobre el propio fuente): sólo `owner_consent` los produce | `test_19` de este PR + censos de P4-S6 |
| B-04 Atajo por `app.security.hitl` (HITL de tools) | el cableado no importa `hitl` ni `comms` (censo por AST); y `hitl.py` no tiene acceso a `owner_consent` | `test_20` de este PR |
| B-05 Propuesta con valores sintéticos | la propuesta se deriva y se re-deriva; `_exigir_evidencia_vigente` compara campo por campo | `test_16` de este PR |
| B-06 Consumo sin binder | el cableado no llama `consumir_aprobacion` ni duplica el paso: lo delega en el binder, que es el único consumidor productivo del censo P4-S6 | `test_19` de este PR + `test_frozen_runtime_p4_s4_approval_authority` |
| B-07 Abrir la emisión sin consumidor | el interruptor tiene **una** definición en todo `sky_claw/` (censo sobre el fuente, no sobre el atributo mutado por monkeypatch) y sigue en `False` | `test_18` de este PR |

**Riesgo residual declarado (R-1):** un llamador **directo** de
`ejecutar_transicion_binder` que no pase por este módulo no verifica la instancia de
MO2 (el scope de 14 campos no la incluye). Hoy es inalcanzable en producción porque
la emisión está cerrada y este módulo es el único emisor; el cierre definitivo exige
un campo nuevo (schema v3) o verificar la procedencia en el binder, y **ambos son
cambios de la puerta productiva** que este PR no hace.

> **Actualización 2026-11-12:** el binder ahora verifica por su cuenta el vínculo con la instancia y el INI
> aprobados (segmento `mo2auth=` de `approval_provenance`, sin esquema nuevo). Ver
> [`2026-11-12_p7_authorization_boundary.md`](2026-11-12_p7_authorization_boundary.md). Este párrafo se
> conserva como estaba al momento de la auditoría.

## 5. Escenarios adversariales obligatorios (cobertura)

| Escenario exigido | Cómo se demuestra |
|---|---|
| consentimiento **reutilizado** | `test_14` (una solicitud resuelta no se reabre) + `test_32` (la reanudación usa la MISMA aprobación, no una nueva) |
| consentimiento **vencido** | El contrato **no** tiene expiración por tiempo (`TIME_BASED_EXPIRATION_REQUIRED = NO`, §37.4). La invalidez es por cambio de evidencia: `test_16` (contenido del Clone cambia ⇒ no se emite) y `test_05` (setup cambia ⇒ no hay evidencia que presentar). Una aprobación abandonada se REVOCA (`approval.revocar_aprobacion`), no expira |
| consentimiento **alterado** | `validar_scope` recomputa el digest en `proponer`/`presentar`/`emitir`/`consumir`; `test_16` cubre el caso del mundo alterado y los censos de P4-S4/S5 el del scope alterado |
| **otra instancia** | `test_07` (registro HKCU apunta a otra instancia ⇒ no hay evidencia) + procedencia ligada a la instancia (F-05) + `test_24` (política) |
| **concurrente** | `test_17`: dos emisiones concurrentes ⇒ exactamente un witness (CAS / `MAX_ISSUED=1` / lock del root) |
| **Steam ACTIVE bajo `STOCK_INDEPENDENT_TRANSITION`** | `test_01`, `test_30` (extremo a extremo con Steam ACTIVE) |
| independencia física / hardlinks | `test_25`: un hardlink en el Clone bloquea aun bajo `STOCK_INDEPENDENT_TRANSITION` (la política no relaja la independencia) |
| **recuperación de aprobaciones consumidas** | `test_32`: fallo entre el burn y el PENDING ⇒ la misma aprobación consumida completa la transición; `test_33`: fallo con PENDING ⇒ no se reanuda, no se re-emite, `REQUIERE_DUENO` |
| `STRICT_PROVIDER_IDLE` intacta | `test_03` (Steam ACTIVE sigue bloqueando) y `test_24` (la sesión no cambia la política) |

## 6. Hallazgos (F)

### F-01 — Ningún módulo productivo construía el scope desde evidencia (ALTO, corregido)
`proponer_aprobacion` acepta los 13 campos del llamador; en los tests de P4/P5 se
llenan con `"d"*64`, `"e"*64`, `"setup-skse-clean"`. Es legítimo para ejercitar el
núcleo, pero **abrir la emisión con ese único productor** habría emitido aprobaciones
sobre evidencia inventada. Este PR agrega el productor que deriva todo desde
verificadores y un test que congela que ningún campo se puede inyectar (la sesión no
tiene parámetro para ellos).

### F-02 — `SourceActivation` no representa un runtime saliente no gestionado (MEDIO, declarado)
§36.3 exige bootstrap íntegramente vacío **o** terna completa. El escenario real de
P7-A5 (MO2 apuntando a un runtime manual Stock-derivado, sin Desired de Sky-Claw) no
es ninguna de las dos cosas. Resolución de este PR: bootstrap vacío (verdadero: «no
había activación **de Sky-Claw**») + el runtime saliente declarado en
`approval_provenance` (digest de la ruta observada, dentro del `approval_scope_digest`
que el dueño aprueba) + rechazo del vacío cuando el runtime saliente vive dentro de
una superficie gestionada. Alternativa rechazada: agregar un estado al enum de
`SourceActivation` (rompe el contrato del journal y de la proyección).

### F-03 — La verificación del Clone no cubría el contenido completo (ALTO, corregido)
`clones.verificar_runtime_clone` (`clones.py:685`) comprueba independencia física,
identidad de runtime y **archivos críticos**. Un archivo agregado o alterado fuera de
esa lista dejaba el Clone `VALID`. Consecuencia: el dueño podía aprobar «este Clone»
y activar un árbol distinto del aprobado. Corrección en el cableado: se inventaría el
árbol, se exige `TreeDigest` observado == Generation registrada (digest, archivos y
bytes) y ese digest entra en `clone_evidence`. Costo declarado: una pasada completa
de hash del Clone por derivación (tres en el camino completo). No se modificó
`verificar_runtime_clone`: cambiar el verificador productivo es otro PR.

### F-04 — `candidate_id` obligatorio sin productor en el camino Stock (MEDIO, declarado)
`PROMOTION` exige `candidate_id` y el camino `adoptar_copia_stock →
publicar_generation_desde_stock → crear_runtime_clone_desde_generation` no crea un
Candidate de P3 (`CANDIDATE_OBJECT = NOT_CREATED`). Se usa un surrogate determinista
`cand_<32 hex>` derivado del contenido verificado. No se declara Candidate; se declara
identificador content-bound.

### F-05 — El scope no liga la instancia de MO2 (ALTO, mitigado en el cableado, residual declarado)
Un scope emitido para el Clone C es reutilizable contra **cualquier** instancia de
MO2 cuyo INI se apunte a C. Mitigación: la instancia (ruta normalizada + SHA-256 del
INI + gamePath observado + config persistido + política + ancla A1 + destino) entra en
`approval_provenance`, que es parte del digest aprobado, y el cableado la re-verifica
antes de emitir y antes de ejecutar. Residuo: llamadores directos del binder (R-1).
*(Cerrado el 2026-11-12: ver [`2026-11-12_p7_authorization_boundary.md`](2026-11-12_p7_authorization_boundary.md).)*

### F-06 — El binder declaraba una reanudación que no siempre existe (MEDIO, corregido en el cableado)
El mensaje de `BinderLockUnavailableError` dice «reintentar con `transition_id=…`
completa la transición». Eso es cierto **sólo si el fallo ocurrió antes de publicar el
`PENDING`**: con `PENDING` publicado, `_crear_pendiente_locked` rechaza
(`AT_MOST_ONE_PENDING_PER_ROOT`) y la aprobación ya está quemada. El cableado
clasifica ambos casos (`REANUDABLE` vs `PENDIENTE_RECUPERABLE` → `REQUIERE_DUENO`) y
lo prueba (`test_32`, `test_33`). No se cambió el mensaje del binder (fuera de alcance).

### F-07 — Dos superficies HITL con autoridades distintas (MEDIO, documentado)
`app/security/hitl.py` (HITLGuard, aprobación de *tools*, en memoria, por token) y
`frozen_runtime/owner_consent.py` (autoridad de aprobación, durable, por digest). No
están conectadas y **no deben** estarlo: un `HITLRequest` aprobado no es un
`ConsentimientoAprobacion`. El cableado no importa `hitl` ni `comms` (test por AST).

## 7. Discrepancias runbook ↔ contratos ↔ comportamiento

| # | Runbook dice | Contrato/código dice | Resolución |
|---|---|---|---|
| C-01 | A5 requiere «habilitar emisión productiva + Steam IDLE + HITL» | Bajo `STOCK_INDEPENDENT_TRANSITION` Steam puede estar **ACTIVE** | El runbook (§9) se actualiza: `STRICT_PROVIDER_IDLE` exige Steam IDLE; `STOCK_INDEPENDENT_TRANSITION` admite ACTIVE con ancla A1, procedencia v2 e independencia física. La emisión sigue cerrada en las dos |
| C-02 | «journal/recovery restaura y la transición se completa» | Burn-first: la aprobación consumida es terminal; con `PENDING` la transición no se completa sola | Se documenta la taxonomía real (F-06) y el cableado la hace explícita |
| C-03 | Aprobación «vence» | `TIME_BASED_EXPIRATION_REQUIRED = NO`: la invalidación es por cambio de evidencia o revocación | Se documenta; el escenario adversarial «vencido» se cubre como «evidencia cambiada» |
| C-04 | El Clone verificado es el que se activa | Hasta F-03 sólo se verificaban críticos | Corregido en el cableado |
| C-05 | La aprobación cubre «la transición a MO2» | El scope cubre el runtime destino, no la instancia | Mitigado (F-05), residual declarado (R-1) |

## 8. Conclusión

- No hay bypass del consentimiento en las vías productivas existentes, y las tres
  vías que podrían aparecer con este PR quedan cerradas por tests (B-04, B-05, B-07).
- El cableado no cambia ningún contrato durable, no toca el esquema del scope, no
  altera el journal, no cambia el binder y no abre la emisión.
- Los residuales R-1 (instancia MO2 fuera del scope), F-02, F-04 y el costo de F-03
  quedan escritos y tienen follow-up propuesto en el plan de activación.

## 9. Revisión adversarial externa (auditor independiente, mismo día)

La revisión independiente del parche encontró tres defectos y una afirmación
documental no reproducible. Los cuatro se reconciliaron con TDD **antes** de
declarar nada listo para integración.

### F-08 — CONFIRMADO (crítico): `reanudar()` no re-verificaba la evidencia aprobada

`ejecutar()` derivaba la evidencia y la comparaba campo por campo; `reanudar()` no.
Reconstruía la aprobación consumida desde el witness, comprobaba `clone_id` y
llamaba al binder **sin volver a mirar el estado actual**: ni la instancia de MO2, ni
el SHA-256 del INI, ni la política, ni el `gamePath` observado.

Reproducción medida sobre el código anterior (rig sintético, mismo Clone destino,
instancia global B con su propio INI):

```text
P1 CONFIRMADO: reanudar desde B terminó con reanudada
INI de B cambió: True
B apunta ahora a: gamePath=@ByteArray(.../FrozenClones/clone-p7-01)
journal: NONE   finalizados: 1
```

Es decir: una aprobación **consumida para la instancia A**, cuya procedencia nombra
A, reanudaba la transición sobre el INI de **B** y la dejaba FINALIZED. La ventana es
peor que cualquier otra: el escenario de reanudación es exactamente una interrupción,
cuando nadie está mirando.

Corrección: `_aprobacion_consumida_durable` devuelve también el witness, y
`reanudar` llama a `_exigir_evidencia_aprobada`, que compara los campos derivados
contra la proyección **aprobada** (`witness.scope_payload`) y rechaza con
`EvidenciaTransicionError` listando los campos distintos. Test `test_34`
(RED: `DID NOT RAISE`; GREEN: rechazo + instancia B, journal, recuperación y
autoridad intactos, y la instancia A todavía puede completar).

### F-09 — CONFIRMADO (alto): `reanudar()` tenía efectos antes de pedir al dueño

Con un `PENDING` publicado, `reanudar()` llamaba a
`binder_recovery.recuperar_transicion_interrumpida()` —que reescribe superficies y
retira registros de recuperación— y a `journal.reconciliar_journal()`, y **después**
lanzaba `ReanudacionRequiereDuenoError`. El operador recibía una excepción sin saber
qué archivos se habían tocado.

Corrección: `reanudar()` en `PENDIENTE_RECUPERABLE` es **sólo diagnóstico** (no escribe
ni un byte) y el mensaje nombra las dos operaciones explícitas del dueño. Test
`test_35`: el INI queda byte a byte igual, el conjunto de registros de recuperación no
cambia (el que dejó el propio binder al fallar sigue siendo el mismo), el journal
sigue `PENDING_PROMOTION` y la recuperación explícita sigue siendo posible.

### F-10 — CONFIRMADO como defensa en profundidad (no explotable de punta a punta)

`_registros_de_clones()` capturaba `FrozenRuntimeError` y seguía: un registro de Clone
ilegible desaparecía de la lista que sostiene la afirmación «el runtime saliente no es
una superficie administrada».

Medición: **el preflight ya falla cerrado** en ese escenario
(`clone_record_corrupt` antes de llegar a la derivación), así que no era explotable de
punta a punta. Se endurece igual: la función ahora lanza `EvidenciaTransicionError` en
lugar de descartar el registro, porque una prueba faltante no puede convertirse en una
prueba superada. Test `test_36` afirma las dos capas (bloqueo del preflight con
`clone_record_corrupt` y fail-closed del helper).

### F-11 — CONFIRMADO (documental): la corrida POSIX de los E2E no era reproducible

La documentación afirmaba que los cuatro tests de extremo a extremo se habían
ejecutado fuera de Windows con el doble POSIX, pero `@SOLO_WINDOWS` los omitía en una
corrida normal: la afirmación no era reproducible por un tercero.

Corrección: opt-in explícito `SKY_CLAW_PR3_E2E_POSIX=1` en el propio marcador, con la
razón y el comando dentro del `reason`. Corrida reproducible: 33 passed en POSIX
(incluye los cuatro E2E y `test_21`, que sigue afirmando que en Windows se usa la
exclusión real).

### Lo que la revisión confirma como correcto

- La dirección arquitectónica: el cableado reutiliza `approval`, `owner_consent`,
  `binder_preflight` y el binder; no crea otra autoridad.
- `EMISION_PRODUCTIVA_HABILITADA = False`, `TRANSPORT_ADAPTER = NOT_WIRED` y la
  primera transición real fuera de alcance.
- El hallazgo F-05 (la instancia no está en el scope) es real y **la reanudación era
  justamente la vía que lo explotaba** (F-08): el residual R-1 queda mitigado también
  en la reanudación, y sólo un llamador directo del binder sigue fuera de esa
  comprobación.

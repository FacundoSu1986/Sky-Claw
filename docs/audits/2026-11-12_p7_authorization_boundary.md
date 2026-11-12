# P7 — Frontera de autorización: INI aprobado e identidad de instancia MO2 (R-1)

> **Estado:** evidencia fechada; no fuente viva por sí sola (ver [`README.md`](README.md)).
>
> **Base:** `origin/main` `3042ab9ad851e8321738f92edcb6ce9210589e2f`.
>
> **Alcance:** cierra el tercer hallazgo de Codex sobre #908
> ([discussion_r4543293593](https://github.com/FacundoSu1986/Sky-Claw/pull/908#discussion_r4543293593))
> y el residual **R-1** declarado en
> [`2026-11-11_p7_a5_pr3_hitl_wiring_scope_audit.md`](2026-11-11_p7_a5_pr3_hitl_wiring_scope_audit.md).
>
> **No cambia:** `EMISION_PRODUCTIVA_HABILITADA = False`, el transporte, la primera transición real
> ni ninguna instalación real. Durabilidad ante corte de energía (#911) queda fuera.

## 1. Hallazgos

| # | Hallazgo | Veredicto | Evidencia |
|---|---|---|---|
| A | El binder valida el INI contra **su propia primera lectura**, no contra el INI que el propietario aprobó | **Confirmado** | RED `test_a1`, `test_a2`, `test_a2b`: `DID NOT RAISE`, la transición con el INI cambiado se ejecutó |
| A′ | Lo mismo en `reanudar()` | **Confirmado** | RED `test_a2` (binder directo con `aprobacion_consumida`) y `test_a2b` (vía `reanudar`) |
| B | R-1: el binder consume una aprobación de la instancia A sobre otra instancia global B | **Confirmado** | RED `test_b1` (consumo directo) y `test_b2` (reanudación desde B) |
| B′ | «El preflight del binder acepta una instancia portable» | **Refutado** | `Mo2InstanceIdentityObservation.is_active` sólo es verdadero para `ACTIVE_GLOBAL`. Lo que faltaba no era *si* la instancia es global activa sino *cuál*: B era la instancia actual del registro |
| C | La identidad de la instancia se verificaba sólo en el adaptador HITL | **Confirmado** | El binder no importaba nada de `approval_provenance` |

Los cinco RED se ejecutaron contra `3042ab9` sin tocar el código: los cinco terminaron en `DID NOT RAISE`.

## 2. Cadena actual (antes del cambio)

```
EvidenciaTransicion ──► approval_provenance ──► ApprovalScope (campo 14, en el digest) ──► IssuanceWitness
       │                                                                                        │
  mo2_ini_sha256 =                                                              scope_payload["approval_provenance"]
  digest_canonico({"ini_bytes_sha256": sha256(bytes del INI)})                                  │
       │                                                                          (el binder NO lo lee)
       ▼
  Consentimiento ──► SesionTransicionGoldenMaster.ejecutar() ──► ejecutar_transicion_binder()
                      _exigir_evidencia_vigente (re-deriva)        │
                                                                   ├─ lee el INI por su cuenta ──► ini_sha_inicial   ← ANCLA
                                                                   ├─ revalidación bajo lock  (== ancla)
                                                                   ├─ snapshot durable        (== bytes revalidados)
                                                                   └─ verificar_huella bajo exclusión (== snapshot)
```

Dónde se calcula, persiste y compromete la huella del INI:

- se **calcula** en `derivar_evidencia_transicion` (`autorizacion_hitl_transicion.py`) leyendo los bytes completos;
- se **compromete** dentro de `approval_provenance` (campo 14 del `ApprovalScope`), que entra al digest del scope;
- se **persiste** en el witness (`scope_payload`), cuyo digest se recomputa al leerlo;
- el propietario ve el resumen legible (`ini=<16 hex>`), pero el hash completo sólo está comprimido en `bind=`: **no es recuperable ni recomputable** por el binder.

Cada eslabón del binder comparaba contra la lectura anterior del propio binder: el cambio de un tercero **antes** de esa primera lectura no era visible para ninguno.

## 3. Contrato propuesto y alternativas

`approval_provenance` pasa a llevar un segmento verificable `mo2auth=<sha256>`
(módulo `sky_claw/local/frozen_runtime/mo2_authorization.py`) que compromete, con un digest canónico:

`instancia (ruta canónica)` · `identidad (active_global)` · `SHA-256 de los bytes completos del INI` · `política de transición` · `runtime saliente`.

El binder lo lee del **witness durable** (no de lo que aporta el llamador), recomputa el token desde **su** observación y exige igualdad:

| Etapa | Dónde | Por qué ahí |
|---|---|---|
| `antes del burn` | tras el preflight inicial, antes de `consumir_aprobacion` | una divergencia ya visible no gasta la aprobación (sigue `ISSUED`) |
| `bajo el lock` | dentro de `participant_lock`, tras la revalidación y antes del snapshot/`PENDING` | cubre el hueco entre el burn y el lock; usa los bytes y la identidad revalidados |
| `bajo la exclusión` | tras adquirir la exclusión del INI, antes de escribir | **el punto de mutación**: compara los bytes que el handle retenido va a mutar |

### Alternativas evaluadas

| | Qué cambia | Costo | Veredicto |
|---|---|---|---|
| **A** Campos nuevos en `ApprovalScope` (v2) | 14→16 campos, digest, censo `APPROVAL_SCOPE_FIELDS`, `CAMPOS_DEL_SCOPE_LIGADOS`, journal, ledger | invalida toda aprobación existente; migración | descartada: desproporcionada |
| **B** Extensión del witness (v4) | esquema cerrado ⇒ v4 + lectura dual | el campo nuevo **no está cubierto por el digest que el propietario aprueba** | descartada: no ata el consentimiento |
| **C** Gramática verificable en el campo 14 ya comprometido | un módulo de bajo nivel + una verificación en el binder | sin esquema nuevo; el segmento entra al digest del scope y por lo tanto a `WHAT_HUMAN_APPROVED == WHAT_WAS_ISSUED` | **elegida** |

Esto es la segunda opción que dejaba escrita la auditoría anterior («verificar la procedencia en el binder»), sin su costo de esquema.

## 4. Compatibilidad (decisión explícita)

- Un segmento **presente** se verifica siempre.
- Un segmento **ausente** se rechaza cuando el vínculo es obligatorio. Tabla cerrada, congelada por igualdad literal en `test_37`:

| `solicitud_binding` presente | transición P7-A5 (política Stock o runtime saliente) | obligatorio |
|---|---|---|
| no | no | **no** (legacy) |
| sí | no | sí |
| no | sí | sí |
| sí | sí | sí |

- **Justificación del único caso tolerado** (sin binding y sin P7-A5): las aprobaciones sin `solicitud_binding` sólo las producen fixtures. `emitir_aprobacion` siempre levanta, el emisor privado `_emitir_aprobacion_bajo_lock` lo invoca únicamente `owner_consent.emitir_aprobacion_autorizada` (que exige el binding completo), y la emisión productiva está cerrada. Esa enumeración está anclada por AST en `test_40`: un llamador nuevo rompe el ancla.
- Witness v1/v2 ya se rechazan fail-closed al leerse: no hay un witness «anterior» que evite el chequeo.
- **No se migra ni se altera ninguna aprobación histórica.** En producción no existe ninguna (emisión cerrada).
- Fixtures de tests de política Stock (`tests/test_frozen_runtime_p7_a5_mo2_executables_transaction.py`, 8 sitios) ahora declaran su vínculo con `procedencia_con_vinculo_mo2` en vez de saltarse el chequeo.

## 5. Invariantes transaccionales

| # | Invariante | Estado | Evidencia |
|---|---|---|---|
| 1 | La aprobación sólo autoriza la instancia MO2 y el INI aprobados | **Verificado ejecutando** | `test_a1..a4`, `test_b1`, `test_b2`, `test_b3_*` |
| 2 | La validación reside en el consumidor | **Verificado ejecutando** | `test_41` (AST) + E2E |
| 3 | `CONSUMED` no vuelve a `ISSUED` | No modificado; suites P4-S4/P4-S6 verdes | sin test nuevo |
| 4 | Una transición abandonada no se ejecuta ni reanuda | Verificado (#909) | `test_58/59/63` del wiring, siguen verdes |
| 5 | Una `FINALIZED` no se abandona | Verificado (#909) | `test_46` del wiring |
| 6 | No se publica `PENDING` sin autoridad durable coherente | **Verificado ejecutando** | `test_a3` (rechazo antes del snapshot/PENDING), `test_41` |
| 7 | Errores de lectura, identidad o digest fallan cerrado | **Verificado ejecutando** | `test_b3_3` (witness manipulado), `test_b3_4`, `test_35` |
| 8 | Journal, tombstones y recovery mantienen su integridad | No modificados; suites verdes | regresión idéntica a la base |
| 9 | No se sobrescribe un cambio externo del INI sin prueba de autoría | **Verificado ejecutando** (rechazos antes de escribir: el cambio del tercero se preserva) | `_rechazo_sin_mutacion` en `test_a4/a5/a6` |
| 10 | Un NO-OP sólo declara éxito con finalización durable | No modificado (`_exigir_finalizacion_durable`) | tests P7-A5 PR2 que lo cubren **no corren en Linux** ⇒ NO VERIFICADO localmente |

## 6. Intercalados investigados

| Intercalado | Cierre | Test |
|---|---|---|
| HITL → binder (cambio del INI en la ventana) | `antes del burn` | `test_a1`, `test_a2b` |
| consumo → `participant_lock` | `bajo el lock` (tras burn queda `CONSUMED` sin `PENDING`: resumible/abandonable por #909) | `test_a3` |
| `participant_lock` → `PENDING` | `bajo el lock` va **antes** del snapshot y del `PENDING` | `test_41` (orden por AST) |
| abandono → reanudación | #909: el binder mira el abandono bajo su lock | `test_59` del wiring |
| lectura INI → exclusión → escritura | cadena + `bajo la exclusión` | `test_a4` (vínculo), `test_a5` (huella vs snapshot), `test_a6` (snapshot vs revalidado) |

**Hallazgo sobre la cadena preexistente.** Mutando el código, los eslabones «`verificar_huella` bajo exclusión» (L1) y «snapshot == bytes revalidados» (L2) **sobrevivían**: ningún test local los discriminaba. Por eso el argumento «por transitividad» no se tomó como prueba: se agregó la verificación directa `bajo la exclusión` y tres tests (`test_a4`, `test_a5`, `test_a6`) que neutralizan el eslabón que de otro modo taparía al probado. Con ellos, L1, L2 y el vínculo mueren por mutación.

## 7. Mutación adversarial

16 mutantes del vínculo + 3 eslabones preexistentes, **todos muertos**:

`V1/V2/V3` (sin verificación en cada etapa) · `D1` (confiar en el digest del llamador) · `O1/O2` (obligatoriedad relajada) · `A1/A2` (adaptador sin segmento / con otro INI) · `T1..T5` (el token ignora INI, instancia, política, runtime saliente o identidad) · `P1` (token presente no obligatorio sin comparar) · `G1/G2` (gramática ambigua o sin delimitar) · `L1/L2/L3` (eslabones previos).

## 8. Riesgos residuales y límites (declarados)

1. **Exclusión real de Windows: NO VERIFICADA localmente.** Los tests corren con el doble POSIX de `exclusion_de_test`; no prueba que ningún otro proceso pueda escribir tras adquirir el handle. Esa propiedad es de P5-C2 (`FILE_SHARE_NONE`) y se ejerce en el CI de Windows.
2. **MO2 no participa de ningún lock de Sky-Claw.** La atomicidad respecto de MO2 no se afirma: el hueco entre la revalidación bajo lock y la adquisición de la exclusión lo cubre la comparación de huella bajo el handle, no un lock.
3. **La obligatoriedad tolera el caso legacy** (sin binding, sin P7-A5). Anclado, justificado e inalcanzable en producción hoy; si se abre la emisión productiva debe revisarse.
4. **La identidad en el token es redundante hoy** con el preflight (`is_active ⇒ ACTIVE_GLOBAL`). Se compromete igual para que la aprobación no dependa de que el preflight siga igual; `test_b3_7` la prueba en el binder.
5. **Rutas léxicas, no físicas.** `clave_de_ruta` normaliza lexicalmente (igual que el binder y el adaptador). Dos rutas distintas a la misma instancia física (junction) producen tokens distintos: se **rechaza** (falla cerrado), nunca se acepta de más.
6. **Un cambio legítimo del INI entre la aprobación y la ejecución se rechaza** (p. ej. MO2 reescribiendo su INI): el propietario debe volver a aprobar. Es el comportamiento buscado.
7. **Rechazo `bajo el lock` tras el burn** deja la aprobación `CONSUMED` sin `PENDING` (ocurre antes de publicarlo): la transición queda resumible o abandonable (#909), no se pierde ni se reutiliza.
8. **Rechazo `bajo la exclusión` tras el burn** ocurre DESPUÉS de publicar el `PENDING`: el rollback restaura las superficies pero no toca el journal, que queda en `PENDING`. Su clasificación corresponde al dueño del journal (`journal.reconciliar_journal`); `recuperar_transicion_interrumpida` no lo escribe.
9. `approval_provenance` deja de ser una referencia puramente opaca: lleva **un** segmento con gramática definida y verificada. Documentado en `mo2_authorization` y en el adaptador.

## 9. Compuertas que no se tocaron

`EMISION_PRODUCTIVA_HABILITADA = False` (una sola definición en `approval.py`; ancla `test_18` del wiring), `HITL_PRODUCTIVE = WIRED_BUT_GATED`, `TRANSPORT_ADAPTER = NOT_WIRED`, `PRIMERA_TRANSICION_REAL = NOT_EXECUTED`.

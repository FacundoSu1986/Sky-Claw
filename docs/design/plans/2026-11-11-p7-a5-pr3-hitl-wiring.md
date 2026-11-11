# P7-A5 PR 3 — Cableado de autorización HITL de la transición Golden Master

> **Estado:** implementado y probado. **Draft PR.** `EMISION_PRODUCTIVA_HABILITADA`
> sigue en `False`; **no** se ejecutó ninguna transición real; **no** se abre la
> emisión productiva; **no** se inicia P7-A6.
>
> **Auditoría de alcance:** [2026-11-11_p7_a5_pr3_hitl_wiring_scope_audit.md](../../audits/2026-11-11_p7_a5_pr3_hitl_wiring_scope_audit.md)

## 1. Problema

P4-S4 (autoridad durable), P4-S5 (binding del scope), P4-S6 (consentimiento HITL
verificado), P4-S7 (identidad durable de la solicitud), P5-C1 (binder transaccional),
P5-C2 (exclusión del INI) y P7-A5 PR 1/PR 2 (preflight Stock + instancia MO2,
transacción de `[customExecutables]`) están implementados y probados **por separado**.
Ningún módulo productivo los encadena:

```text
proponer_aprobacion            → sin llamador productivo
presentar / responder          → HITL_PRODUCTIVE = NOT_WIRED
emitir_aprobacion_autorizada   → cerrada por interruptor
ejecutar_transicion_binder     → sin emisor que lo alimente
```

Abrir el interruptor en ese estado habría emitido aprobaciones sobre evidencia
inventada (F-01 de la auditoría) y dejado `ISSUED` sin salida operativa.

## 2. Arquitectura del cableado

```text
                    ┌──────────────────────────────────────────────────────────┐
                    │ SesionTransicionGoldenMaster                             │
                    │                                                          │
 derivar_evidencia  │  preparar ──► proponer ──► presentar ──► entregar(puerto) │
 (read-only)        │      │                                       ▲           │
                    │      │                    identidad del TRANSPORTE        │
                    │      │                    (verificar_identidad_telegram) │
                    │      ▼                                                   │
                    │  decidir_y_emitir ──► responder ──► [re-derivar y comparar]│
                    │                                    └─► emitir_aprobacion_  │
                    │                                        autorizada (CERRADA)│
                    │      ▼                                                   │
                    │  ejecutar ──► ejecutar_transicion_binder ──► verificar    │
                    │  reanudar ──► (misma aprobación consumida) ──► verificar  │
                    └──────────────────────────────────────────────────────────┘
```

Módulo nuevo: `sky_claw/local/frozen_runtime/autorizacion_hitl_transicion.py`
(1.479 líneas, docstrings «por qué» incluidas). No se modifica **ninguna** línea de `approval.py`,
`owner_consent.py`, `journal.py`, `binder_recovery.py`, `runtime_clone_binder.py`,
`binder_preflight.py` ni `provisioning.py`.

### 2.1 Evidencia derivada (no aportada)

`derivar_evidencia_transicion` corre el preflight (read-only) y verifica la cadena de
identidad completa (`ADR 0012` §35.2/§36.3, auditoría §3): preflight `READY`, política
efectiva == política pedida, `ACTIVE_GLOBAL` de MO2, Clone `PROVISIONED` con
`verified == intended` setup, `verificar_runtime_clone == VALID`,
`verificar_generation == VALID`, `verificar_runtime_setup` content-bound **y**
`TreeDigest` observado del Clone == Generation registrada (F-03, digest + archivos +
bytes). De ahí salen los 9 campos de evidencia del scope; `approval_provenance` liga
instancia MO2 + SHA-256 del INI + gamePath observado + config persistido + política +
ancla A1 + destino + digests (F-05).

### 2.2 Puertas del cableado (G1–G7)

| Puerta | Qué impide | Test |
|---|---|---|
| G1 | que el llamador aporte cualquier campo del scope | `test_01`, `test_02` |
| G2 | que la presentación no sea la del registro HITL | `test_10` |
| G3 | que el cableado abra la emisión (y que se pueda abrir sin consumidor) | `test_11`, `test_18` |
| G4 | reutilización, cross-sesión, cross-root del consentimiento | `test_13`, `test_14`, `test_15` |
| G5 | que se ejecute una aprobación de un estado del mundo anterior | `test_16`, `test_32` |
| G6 | una segunda transición con otra aprobación mientras hay una en curso/reanudable | `test_22`, `test_23`, `test_32`, `test_33` |
| G7 | degradar/ascender la política y cualquier requisito de independencia física | `test_03`, `test_24`, `test_25` |

### 2.3 Reanudación honesta

```text
fallo ANTES de publicar el PENDING   → REANUDABLE      → reanudar() completa con la MISMA aprobación
fallo DESPUÉS de publicar el PENDING → PENDIENTE_REC.  → recuperar superficies + reconciliar journal
                                                          → REQUIERE_DUENO (la aprobación ya se gastó)
```

No se inventa una segunda emisión para «recuperar» un burn (burn-first, §37.2). El
`transition_id` de reanudación es el del tombstone durable, no uno nuevo.

## 3. Implementación mínima justificada

| Decisión | Por qué es la mínima | Alternativa rechazada |
|---|---|---|
| Módulo nuevo en vez de tocar el binder | el binder es una puerta productiva auditada; agregarle autorización habría cambiado su contrato | cablear la emisión dentro del binder |
| Emisión por `emitir_aprobacion_autorizada` | es la superficie productiva y la que falla cerrada | emitir con `_emitir_aprobacion_bajo_lock` (bypass del gate) |
| `approval_provenance` como compromiso legible+digest | el esquema del scope está congelado (14 campos, digest con §38.5) | campo 15 / schema v3 (rompe todo lo emitido) |
| `candidate_id` surrogate content-bound | `PROMOTION` lo exige y el camino Stock no crea Candidates | fabricar un `cand_` aleatorio (no verificable) |
| Ventana de reanudación = burn sin PENDING | es la única que el contrato permite completar | «re-emitir y reintentar» (dos autorizaciones de lo mismo) |
| Sin adaptador de transporte | una segunda superficie de identidad sin consumidor es el defecto que P4-S6 cerró | enviar Telegram desde el cableado |
| Sin exportar en `__init__` | evita arrastrar `runtime_clone_binder` al import del paquete antes de que exista el adaptador | exportar ahora |

## 4. TDD (RED → GREEN)

| Commit | Contenido | Estado |
|---|---|---|
| `test(frozen-runtime): … RED` | 30 tests (26 ejecutables en POSIX + 4 de mutación real de INI) + `tests/_rig_p7_a5.py` | **RED**: `ModuleNotFoundError: autorizacion_hitl_transicion` |
| `feat(frozen-runtime): … GREEN` | el módulo | **GREEN**: `26 passed, 4 skipped` |
| `docs(...)` | auditoría, este plan, runbook §9 | — |

Los 4 tests de extremo a extremo se ejecutaron además **fuera de Windows** con un doble
POSIX explícito de la exclusión de archivos compartidos (`_ExclusionPosix`, aplicado
sólo si `os.name != "nt"`, con `test_21` que afirma que en Windows no se aplica). En
Windows CI corre la exclusión **real**; la lógica del cableado ya quedó verificada
contra el binder completo.

## 5. CI

- Gates existentes, sin cambios de configuración: `ruff check`, `ruff format --check`,
  `mypy`, `pytest` en `windows-latest` (py3.11 y py3.12), CodeQL.
- Suite local: `tests/test_frozen_runtime_*.py` → **1.333 passed / 58 skipped**;
  los 51 fallos restantes son **preexistentes y exclusivos de POSIX** (la exclusión de
  Win32 no existe fuera de Windows; verificado con `git stash`: mismo número y mismos
  tests en `main`).
- Tests nuevos: 26 ejecutables en cualquier SO + 4 Windows-only.

## 6. Riesgos residuales

| ID | Riesgo | Impacto | Mitigación / follow-up |
|---|---|---|---|
| R-1 | El scope no liga la instancia MO2 para llamadores **directos** del binder | una aprobación podría aplicarse a otro INI | mitigado por procedencia + re-verificación en el cableado; cierre definitivo = campo de scope v3 o verificación en el binder (PR propio, cambia la puerta productiva) |
| R-2 | `SourceActivation` vacío para runtime saliente no gestionado (F-02) | la representación no distingue «sin runtime» de «runtime no gestionado» | declarado en procedencia; follow-up: estado explícito en el contrato |
| R-3 | `candidate_id` surrogate (F-04) | no es un Candidate de P3 | follow-up: productor real en el provisionamiento |
| R-4 | Costo de F-03: hashing completo del Clone por derivación (3 por transición) | minutos en un árbol de ~17 GiB | aceptable para una operación aprobada y supervisada; follow-up: caché de inventario con invalidación por `usn`/mtime |
| R-5 | `TRANSPORT_ADAPTER = NOT_WIRED` | no hay entrega ni decisión reales por Telegram | PR 4 (adaptador), con identidad acuñada por `verificar_identidad_telegram` desde el update crudo |
| R-6 | `EXCLUSION_WITH_MO2 = NOT_ESTABLISHED` (heredado P5-C1) | ventana entre la última revalidación y el `os.replace` | sigue siendo precondición de la primera transición real; no se relaja acá |
| R-7 | `POWER_LOSS_GUARANTEE = NOT_CLAIMED` | sin WAL ni fsync de directorio | heredado y sin cambios |

## 7. Plan de activación (PR 4, **no** este PR)

Prerrequisitos, en orden, cada uno con su gate:

1. **Adaptador de transporte** (`TRANSPORT_ADAPTER`): presentar por Telegram con los
   14 campos + procedencia; recibir el update y acuñar la identidad con
   `verificar_identidad_telegram` (operador configurado, chat privado, sin reenvío);
   responder APROBAR/RECHAZAR/CANCELAR. Tests: replay, reenvío, chat de grupo, sujeto
   distinto, sesión distinta.
2. **Primera transición en seco sobre el rig Windows** con `EMISION_PRODUCTIVA_HABILITADA`
   todavía en `False` (se fuerza el interruptor sólo en el test) y evidencia de:
   witness + binding + tombstone + journal `FINALIZED` + superficies verificadas.
3. **PR de activación**: una línea (`EMISION_PRODUCTIVA_HABILITADA = True`) + revisión
   de los censos (el test `test_18` de este PR cambia de valor esperado) + checklist
   del runbook A1–A5 + aprobación explícita del propietario.
4. **Ventana controlada** de la primera transición real: backup con hashes (§5 del
   runbook), MO2 cerrado, Steam en el estado admitido por la política elegida, journal
   limpio, `diagnosticar_estado_durable == LIMPIO`.

### Rollback

| Escenario | Acción | Evidencia que se preserva |
|---|---|---|
| Antes de emitir | descartar la sesión (`descartar_todo`) | nada durable escrito |
| Emitida y no consumida | `revocar_aprobacion` | witness + tombstone `REVOKED` |
| Consumida, sin `PENDING` | `reanudar(transition_id)`; si no se quiere completar, `recuperar_transicion_interrumpida` + `reconciliar_journal` y **nueva** aprobación | witness + tombstone `CONSUMED` + registro de recuperación |
| `PENDING` publicado | `recuperar_transicion_interrumpida` (restaura superficies, pide dueño) + `reconciliar_journal` | registro de recuperación + `recovery_evidence/` |
| Activación abierta y algo falla | revertir el PR de activación (`EMISION_PRODUCTIVA_HABILITADA = False`) y repetir el procedimiento de la fila anterior | historial de emisiones |

Ningún rollback borra evidencia: los stores son inmutables y no-clobber (§38.2/§40.2).

## 8. Límites respetados

- `EMISION_PRODUCTIVA_HABILITADA = False` (única definición; `test_18`).
- Sólo fixtures y rigs sintéticos en `tmp_path`.
- No se tocó MO2 real, Steam, Stock, Generation, Clone ni configuración productiva.
- No se ejecutó la primera transición real.
- Sin merge automático; sin P7-A6.

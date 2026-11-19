# Política de Cierre — Anti-Mejora Infinita (Sky-Claw)

> **Audiencia:** agentes de IA y humanos que supervisan cambios asistidos.
>
> **Estado:** norma de proceso vigente. El ancla `tests/test_completion_policy_invariant.py`
> verifica que el texto no desaparezca ni se deforme; **no** verifica que un cierre concreto
> la cumpla (ver §9).
>
> **Fuentes canónicas:** [`AGENTS.md`](../../AGENTS.md) (sección «Política de cierre y Semáforo
> Anti-Mejora Infinita») y este documento. Si difieren, manda `AGENTS.md`.
>
> **Última verificación:** 2026-11-19 sobre `origin/main` `4369994f`.

## 1. Principio rector

El objetivo del agente es cumplir el contrato de aceptación acordado, no optimizar el repositorio indefinidamente. Una entrega verificada es un **punto de parada**, no el inicio automático de otra auditoría ni de una cadena de PRs.

Orden estricto de prioridad:
`seguridad > integridad de datos > correctitud (incluidos hermanos del mismo cambio y fail-closed) > tests/anclas AST > mantenibilidad > rendimiento`.

## 2. Contrato de Terminado (Definition of Done)

Antes de editar archivos, fijar o confirmar desde la tarea/issue:

- **Objetivo único y `Write-Set` autorizado** (un PR = un tema; respetar archivos puente transversales y worktree aislado).
- **Criterios de aceptación verificables** y si exigen prueba en **rig real Windows/MO2** o bastan **CI/headless**.
- **Hermanos y anclas AST alcanzados** (superficie GUI vs. Agente LLM, runners/servicios gemelos y tests de invariantes aplicables).
- **Fuera de alcance explícito** (qué archivos o deudas previas NO se tocan).

Está prohibido ampliar estos criterios a mitad de ejecución para justificar mejoras opcionales. Cualquier expansión requiere autorización explícita del propietario, salvo contención mínima ante un `P0` confirmado.

## 3. Semáforo de Decisión y Clasificación de Hallazgos

Todo hallazgo requiere evidencia (`archivo:línea`, escenario concreto de fallo e impacto). La severidad depende del impacto demostrado, nunca de cuántos bots o modelos repitan la opinión.

| Semáforo | Recomendación | Severidad | Qué incluye en Sky-Claw | Acción obligatoria |
| --- | --- | --- | --- | --- |
| **🔴 ROJO** | **ALTA (Frenar)** | **P0 — Crítico** | Riesgo grave de seguridad, corrupción/pérdida de datos, fuga fuera de raíces administradas, uso de `shutil.rmtree` o violación Clean-Room. | Detener, contener y corregir antes de seguir. |
| **🔴 ROJO** | **ALTA (Frenar)** | **P1 — Bloqueante** | Defecto demostrado que viola el contrato actual, falso verde (`exit_code == 0` sin validar artefacto/log), **hermano olvidado del mismo cambio**, fallo real en zona exenta de `mypy` (`sky_claw/app/**`, `tools_installer.py`), test/ancla AST rota o falta de prueba de rig exigida por el contrato original. | Corregir antes de aprobar. Prohibido declarar `DONE`. |
| **🟡 AMARILLO** | **MEDIA (Decidir)** | **P2 — No bloqueante** | Defecto real acotado que no rompe la aceptación actual, mejora arquitectónica válida pero fuera del `Write-Set`, o limitación conocida de rig Windows/MO2 declarada como deuda aceptada. | Registrar en el cierre. El propietario decide si va a backlog/issue aparte. No implementar en este PR. |
| **🟢 VERDE** | **BAJA (Cerrar)** | **P3 — Opcional / Ruido** | Estilo, refactors cosméticos, rendimiento no exigido, renombrar tests, comentarios «Trivial / Low value / Quick win» sin bug demostrado, hipótesis `NO_VERIFICADO`, o rojo por caída de modelo gratuito en Qodo/PR-Agent. | Descartar con 1-2 líneas de evidencia y **detenerse**. No generar commits nuevos. |

## 4. Regla contra mejoras indefinidas y presupuesto de ciclos

Cuando el cambio cumple los criterios de aceptación:

1. Correr gates obligatorios (`ruff check`, `ruff format --check`, `mypy`, `pytest` + invariantes AST) y confirmar que el ROJO previo pasó a VERDE.
2. Ejecutar **una (1)** revisión adversarial independiente acotada al diff del PR.
3. Agrupar y corregir **en una (1) sola ronda** únicamente los `🔴 ROJO (P0/P1)` confirmados, y revalidar regresiones.
4. Emitir el informe de cierre y **DETENER** la edición.

- **Regla de costo de CI:** Cada push extra cancela la corrida de Windows (~40 min). Nunca hacer commits aislados por sugerencias `🟢 VERDE (P3)`.
- **Escalamiento:** Si tras 1 revisión + 1 ronda de corrección siguen abiertos defectos `🔴 ROJO`, frenar y escalar al propietario o Tech Lead en estado `BLOCKED`. El límite de rondas jamás autoriza ocultar fallos ni declarar `DONE` en falso.

## 5. Triage de bots y otros agentes (CodeRabbit, Copilot, Codex, Qodo)

Una sugerencia automática no es un defecto hasta probarse. Ante cada comentario:

1. **CLAIM → EXTRACT → DOUBT → RECONCILE:** Leer el código real (`archivo:línea`) y, cuando corresponda, reproducir antes de aceptar.
2. Exigir para marcarlo `🔴 ROJO`: ruta exacta, mecanismo reproducible o prueba estática concluyente, impacto verificable y por qué rompe el objetivo actual.
3. **Falsos rojos conocidos de infraestructura:**
   - Error de proveedor/cuota o YAML inválido en Qodo/PR-Agent (`:free`) → no bloquea el código; reportar como fallo del revisor.
   - Job agregador `Tests (Pytest)` en rojo únicamente porque un push nuevo canceló Windows (`cancelled`, no `failure`) → esperar la corrida vigente, no reabrir el código.
4. Responder cada hilo con resultado (`Corregido en <sha>` o `Descartado — 🟢 P3 / No aplica porque <evidencia>`). No modificar código correcto solo para silenciar un bot.

## 6. Prohibición de crecimiento accidental (Scope Creep)

Prohibido sin orden explícita del propietario:

- Refactorizar módulos vecinos o tocar archivos puente transversales fuera del `Write-Set`.
- Abrir PRs encadenados o crear GitHub Issues nuevas automáticamente por hallazgos `🟡 P2` o `🟢 P3`.
- Repetir auditorías completas sobre un PR sin cambio material ni evidencia nueva.
- Reabrir una tarea cerrada únicamente porque otro modelo opina que "podría quedar más limpio".
- Convertir limitaciones documentadas de rig físico en bloqueos infinitos si el contrato del PR las excluyó explícitamente.

## 7. Estados de cierre y regla de reapertura

Emitir exactamente uno:

- **`DONE`** (`🟢 VERDE`): Contrato cumplido, gates obligatorios en verde sobre el SHA entregado, cero `🔴 P0/P1` abiertos.
- **`DONE_WITH_LIMITATIONS`** (`🟡 AMARILLO` o `🟢 VERDE` según impacto): Contrato cumplido sin `🔴 P0/P1` bloqueantes; quedan limitaciones explícitas y compatibles con el alcance (ej. smoke pendiente en rig Windows declarado desde el inicio).
- **`BLOCKED`** (`🔴 ROJO`): Existe un `P0/P1`, falta evidencia obligatoria del contrato (incluida validación de rig si el PR la exigía) o falló un gate obligatorio.
- **`OUT_OF_SCOPE`** (`🟡 AMARILLO` / `🟢 VERDE`): Propuesta válida pero ajena a la tarea actual; se registra y no se implementa.

**Reapertura:** Una tarea cerrada solo se reabre por (1) regresión reproducida, (2) defecto P0/P1 demostrado (vulnerabilidad o fallo de integridad), (3) incumplimiento del contrato original (un criterio original descubierto sin cumplir) o (4) nueva orden explícita del propietario.

## 8. Tarjeta obligatoria de cierre (para el propietario)

Todo cierre de tarea o revisión debe empezar con este bloque en español simple, sin jerga innecesaria:

```markdown
### Semáforo de Cierre: [🔴 ROJO — ALTA | 🟡 AMARILLO — MEDIA | 🟢 VERDE — BAJA]
- **Estado:** `DONE` | `DONE_WITH_LIMITATIONS` | `BLOCKED` | `OUT_OF_SCOPE`
- **Recomendación al propietario:** [1 frase directa: "Listo para mergear" / "Frenar y corregir X" / "Mergear y decidir si anotamos Y en backlog"]
- **SHA / Versión verificada:** `<commit>` (recordar `Closes #NNN` en texto plano sin backticks si aplica)
- **Honestidad de verificación:**
  - *Ejecutado:* [comandos/tests corridos y resultado real]
  - *Leído en código:* [qué se auditó sin ejecutar]
  - *No verificado:* [ej. rig real Windows/MO2/Skyrim si aplica]
- **Hallazgos clasificados:**
  - 🔴 Bloqueantes (P0/P1): [Ninguno / lista con archivo:línea]
  - 🟡 Diferidos / Limitaciones (P2): [Ninguno / lista breve]
  - 🟢 Descartados (P3 / ruido de bots): [Ninguno / motivo en 1 línea]
- **Por qué corresponde detenerse (o seguir) ahora:** [justificación basada en el contrato original]
```

## 9. Qué verifica el repo y qué no

- **Verificado por test** (`tests/test_completion_policy_invariant.py`): que la sección exista
  en `AGENTS.md` y apunte a este documento; que el semáforo (colores, recomendaciones y
  severidades), los estados finales con su color, la prioridad, las condiciones de reapertura
  y los campos de la tarjeta de §8 estén congelados por igualdad literal; que los enlaces y
  las rutas citadas existan; que los «falsos rojos» de §5 (`Tests (Pytest)`, el modelo
  `:free` de Qodo) y la zona exenta de `mypy` de §3 sigan existiendo en `ci.yml`,
  `qodo-merge-adversarial.yml` y `pyproject.toml`; y que el documento esté indexado en
  [README.md](README.md).
- **No verificado:** que un agente clasifique bien un hallazgo, que un cierre concreto
  respete el presupuesto de §4 o que la tarjeta de §8 se emita. Eso depende de la revisión del
  propietario sobre el cuerpo del PR. Un test verde sobre el ancla no dice nada sobre el
  próximo cierre.

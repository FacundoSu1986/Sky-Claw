# Política de cierre y Semáforo Anti-Mejora Infinita

> **Audiencia:** agentes de IA y humanos que supervisan cambios asistidos.
>
> **Estado:** norma de proceso vigente. El ancla `tests/test_completion_policy_invariant.py`
> verifica que el texto no desaparezca ni se deforme; **no** verifica que un cierre concreto
> la cumpla (ver §8).
>
> **Fuentes canónicas:** [`AGENTS.md`](../../AGENTS.md) (sección «Política de cierre y Semáforo
> Anti-Mejora Infinita») y este documento. Si difieren, manda `AGENTS.md`.
>
> **Última verificación:** 2026-11-19 sobre `origin/main` `4369994f`.

## 1. Objetivo y alcance

Este documento fija **cuándo una tarea está cerrada** y **qué hacer con los hallazgos que
aparecen al cerrarla** (de un revisor humano, de un bot o del propio agente). Aplica a todo
cierre y a todo triage de hallazgos, incluidos los de `qodo-merge-adversarial.yml` y
PR-Agent.

**Principio rector.** Cumplir el contrato de aceptación acordado; no perseguir perfección
indefinida ni ampliar el alcance por iniciativa propia.

No reemplaza ninguna norma de fondo: la regla de los hermanos de `AGENTS.md`, el estándar de
merge ([merge_and_conflict_resolution.md](../development/merge_and_conflict_resolution.md)) y
la jerarquía de fuentes ([source_of_truth.md](../documentation/source_of_truth.md)) siguen
vigentes. Esta política decide **cuánto** se sigue trabajando; esas normas deciden **qué**
cuenta como correcto.

## 2. Cuándo leerla

1. **Antes de implementar:** para fijar el contrato de aceptación y el `Write-Set` contra los
   que se va a medir el cierre.
2. **Antes de declarar una tarea cerrada:** para clasificar cada hallazgo abierto con el
   semáforo de §4 y elegir un estado final de §6.

## 3. Prioridad

Cuando dos criterios compiten, gana el de la izquierda:

> seguridad > integridad de datos > correctitud (incluidos hermanos del mismo cambio y
> fail-closed) > tests/anclas AST > mantenibilidad > rendimiento

## 4. Semáforo obligatorio

Es obligatorio en **todo cierre** y en **todo triage de bots**. Cada hallazgo recibe un color
y, con él, una recomendación y una acción.

| Color | Recomendación | Criterio | Acción |
|---|---|---|---|
| 🔴 ROJO | ALTA — frenar y corregir | P0/P1 confirmado con evidencia: seguridad, pérdida o corrupción de datos, falso verde, hermano omitido del mismo alcance, falla en zona exenta de `mypy`, o gate obligatorio roto. | Corregir. **Prohibido declarar `DONE`** mientras exista. |
| 🟡 AMARILLO | MEDIA — decisión del propietario / diferir | P2 real pero no bloqueante; limitación conocida de rig real Windows/MO2 compatible con el alcance; mejora útil fuera del `Write-Set`. | Documentar. **No** abrir otro PR ni ampliar el actual sin autorización. |
| 🟢 VERDE | BAJA — cerrar y detenerse | P3: estilo, refactor opcional, renombrar tests, «Trivial / Quick win» sin defecto demostrado, hipótesis `NO_VERIFICADO`, o fallo transitorio de proveedor en Qodo/PR-Agent. | Justificar brevemente el descarte y **no commitear**: evita reiniciar ~40 min de CI Windows. |

Notas de lectura:

- 🔴 exige **evidencia**: un hallazgo sin reproducción ni lectura del código real no es 🔴.
- «Hermano omitido del mismo alcance» es el mismo defecto que describe la sección «La regla
  que más se viola» de `AGENTS.md`; un hermano **fuera** del `Write-Set` es 🟡, no 🔴.
- Un hallazgo 🟢 no se «arregla por si acaso»: cada commit reinicia el CI.

## 5. Verificación de bots y agentes

Antes de tocar nada por un hallazgo ajeno, aplicar `CLAIM → EXTRACT → DOUBT → RECONCILE`
contra el código real del árbol actual. Un hallazgo no verificado queda como
`NO_VERIFICADO` y, sin defecto demostrado, es 🟢.

Este documento fija el orden y la obligación, no el procedimiento de cada paso. Un ejemplo
de aplicación en el repo es
[2026-11-15_xedit_bridge_acceptance.md](../validation/2026-11-15_xedit_bridge_acceptance.md).

### Presupuesto ordinario

- **1** revisión adversarial acotada al cambio, y
- **1** ronda de corrección y revalidación.

Si después de eso persisten bloqueantes (🔴), **detener y escalar** al propietario; no abrir
una segunda ronda por iniciativa propia.

## 6. Estados finales

Todo cierre termina en **exactamente uno** de estos cuatro estados:

| Estado | Significado |
|---|---|
| `DONE` | Contrato de aceptación cumplido y sin 🔴 abiertos. |
| `DONE_WITH_LIMITATIONS` | Contrato cumplido; quedan 🟡 documentados y declarados. |
| `BLOCKED` | Hay un 🔴 que no se puede resolver dentro del presupuesto o del alcance. |
| `OUT_OF_SCOPE` | El pedido (o el hallazgo) cae fuera del contrato o del `Write-Set` acordado. |

No se inventan estados intermedios («casi listo», «listo salvo…»): lo que falta es un 🟡 y
va en `DONE_WITH_LIMITATIONS`, o es un 🔴 y va en `BLOCKED`.

## 7. Reapertura

Un cierre solo se reabre por:

1. una **regresión reproducida**;
2. un defecto **P0/P1 demostrado**;
3. el **incumplimiento del contrato original**; o
4. una **nueva orden explícita del propietario**.

Un comentario de bot posterior, una preferencia de estilo o una mejora posible no reabren
nada.

## 8. Qué verifica el repo y qué no

- **Verificado por test** (`tests/test_completion_policy_invariant.py`): que la sección exista
  en `AGENTS.md` y apunte a este documento; que los colores, los estados finales, la
  prioridad y las condiciones de reapertura estén congelados por igualdad literal; que los
  enlaces y las rutas citadas existan; y que el documento esté indexado en
  [README.md](README.md).
- **No verificado:** que un agente clasifique bien un hallazgo, ni que un cierre concreto
  respete el presupuesto. Eso depende de la revisión del propietario sobre el cuerpo del PR.
  Un test verde sobre el ancla no dice nada sobre el próximo cierre.

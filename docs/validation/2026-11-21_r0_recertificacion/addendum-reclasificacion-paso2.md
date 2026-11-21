# Addendum: reclasificación del paso 2 de R0-2 contra el preregistro

> Agregado en la revisión del PR de evidencia. No modifica `final-report.md` ni `PREREGISTRO.md` (sellado).

`PREREGISTRO.md`, fila 2, fija como esperado del paso 2: **`rc≠0`**, el rechazo con su texto, 0 jobs y sin cuarentena.

Observado (`r0_2/paso2/run_01/meta.json`, `hallazgo_proceso_colgado.json`): el rechazo, el texto, 0 jobs y 0 cuarentenas se
cumplen, pero **no hubo `rc`**: el proceso no terminó y hubo que matarlo desde afuera (hallazgo 1 del informe).

| Paso | Veredicto en `final-report.md` | Veredicto contra el contrato preregistrado |
|---|---|---|
| 1 | PASS | **PASS** |
| 2 | PASS (con hallazgo 1) | **FAIL** — el rechazo del bridge se verificó (parcial satisfactorio); el criterio `rc≠0` no se cumplió |
| 4 | PASS (con hallazgo 1) | **PASS** — el preregistro no pide `rc` en este paso; el `rc=1` viene del corte externo a los 180 s y no acredita una salida del producto |
| 3 | PASS | **PASS** |

Este veredicto es el que manda para el HOLD. La identidad del bundle quedó demostrada en el rig (rechazo con el digest alterado,
digest fijo al cargar, aceptación tras reinstalar y reabrir). El `FAIL` del paso 2 es el mismo defecto del hallazgo 1:
`--mode vfs-health` no termina después de un rechazo. Lo cierra el PR correctivo aparte, con su test en rojo y una re-corrida del
paso 2 en el rig.

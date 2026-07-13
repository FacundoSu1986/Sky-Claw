# ADR 0003 — Daemons runtime: se mantiene el fail-fast colectivo (sin restart individual ni backoff a nivel supervisor)

**Fecha:** 2026-07-13
**Estado:** Aceptada
**Contexto de origen:** ítem 6 del roadmap de arbitraje de auditorías
("restart individual + backoff"; arbitrado MEDIO/opcional — "el diseño es
defendible, evaluar antes de tocar"). Evaluación OODA + Tree of Thoughts
contra `main @ af35215`.

## Contexto

El runtime de daemons tiene hoy una defensa en **dos capas**, deliberada
(comentarios ARC-01 + SUP-05 + H-2 en `supervisor.py`):

1. **Recuperación por-iteración, dentro de cada daemon.** Los errores
   operativos (I/O, DB, psutil, event bus) se absorben y loguean sin matar el
   loop; el `sleep` del intervalo actúa como rate-limit/backoff implícito:
   - `telemetry_daemon.py::_telemetry_loop` — `while True` con
     `except CancelledError: raise` + `except Exception: log`.
   - `watcher_daemon.py::_watch_loop` — mismo patrón.
   - `maintenance_daemon.py::_pruning_loop` — variante: el loop solo re-lanza
     `CancelledError`; la absorción vive en los helpers `_checkpoint_tick` y
     `_pruning_check`, cada uno con su propio `except Exception`.

2. **Fail-fast colectivo, en el supervisor.**
   `supervisor.py::_run_daemons_and_interface` corre los 3 daemons + la
   interfaz bajo `asyncio.wait(FIRST_COMPLETED)`: si una task termina con
   excepción no-Cancelled, cancela al resto y la propaga. Si la interfaz
   retorna normalmente (error de red ya absorbido por
   `_run_interface_isolated`), apaga los daemons con gracia. El `finally`
   cancela y drena todas las tasks en ambos casos.

La consecuencia clave: el fail-fast **solo puede disparar** si una excepción
*escapa* la capa 1. Enumerando la superficie desprotegida real de cada loop:

- telemetry: `psutil.Process()` y el cebado de `cpu_percent` (una vez, al
  arrancar) + `asyncio.sleep`.
- watcher: un f-string + `asyncio.sleep`.
- maintenance: `asyncio.sleep`, un contador y un módulo.

No hay ahí ninguna fuente realista de fallo **transitorio**. Lo único que
puede escapar es un bug determinístico en el andamiaje del loop, un bug en el
propio handler, o una `BaseException` (p. ej. `MemoryError`). Ninguna de esas
tres clases se recupera reiniciando la task.

## Alternativas evaluadas (Tree of Thoughts)

### (a) No cambiar: fail-fast colectivo + recuperación interna — **elegida**

- Lo recuperable ya se recupera en la capa 1, con backoff implícito.
- Lo que escapa es por construcción un bug: conviene que crashee **visible**
  (crash-only design). Sky-Claw es una app de escritorio mono-usuario que
  protege la instalación de mods del usuario; un watcher degradado
  reiniciándose en silencio significa que el usuario cree que los cambios
  externos al modlist se detectan cuando no es confiable. Crash visible →
  el usuario reinicia la app y el bug se reporta.
- La decisión ya está anclada por tests
  (`tests/test_supervisor_taskgroup_reraise.py::TestRunDaemonsAndInterface`
  y `test_daemon_run_propaga_excepcion_del_loop`): cualquier cambio al
  fail-fast rompe la suite, no puede degradarse en silencio.

### (b) Restart individual + backoff exponencial acotado a nivel supervisor — descartada

- **Beneficio ≈ cero:** el único disparador posible es un bug determinístico
  → el restart re-crashea de inmediato → tras N reintentos se cae en
  fail-fast igual, habiendo solo *demorado* el crash y ensuciado los logs.
  Para `BaseException` (`MemoryError`) reiniciar es activamente incorrecto.
- **Enmascara bugs:** convierte un crash ruidoso en ruido de log; el daemon
  "sigue corriendo" desde la perspectiva del usuario mientras no hace nada
  útil (restart-loop acotado, pero loop al fin).
- **Costo donde más duele:** la máquina de restart debe distinguir el cancel
  de shutdown de un crash, no re-spawnear tasks después de que el `finally`
  empezó a cancelar (carrera entre el retorno normal de la interfaz y la
  muerte de un daemon), reconstruir el set de `asyncio.wait` en cada vuelta,
  y llevar contadores/backoff por daemon con logging correlacionado. Es una
  máquina de estados async nueva para defender estados "imposibles": tiene
  más probabilidad de contener un bug que el código que protege.

### (c) Híbrido: restart solo para daemons idempotentes/no-críticos — descartada

- Misma maquinaria que (b) **más** una taxonomía de criticidad a mantener.
- El único daemon que califica como restart-safe (telemetry: stateless,
  idempotente, no-crítico) es justamente el que menos valor tiene mantener
  vivo a toda costa. Peor ratio costo/beneficio de las tres ramas.

## Decisión

**Se recomienda no cambiar** (rama a): se mantiene el fail-fast colectivo de
`_run_daemons_and_interface` tal como está, sin restart individual ni backoff
a nivel supervisor. El ítem 6 del roadmap se cierra como "evaluado — sin
cambio de código".

## Consecuencias

- Todo daemon nuevo debe seguir el patrón de la capa 1: `while True` con
  absorción por-iteración (`except Exception` que loguea) y re-raise de
  `CancelledError`, con el `sleep` del intervalo como backoff implícito. El
  fail-fast del supervisor asume esa invariante.
- Los tests ancla de `tests/test_supervisor_taskgroup_reraise.py` quedan como
  guardián del contrato: no relajarlos sin revisar este ADR.

## Criterio de reversión

Reabrir la evaluación (empezando por la rama (c), acotada al daemon
específico — no por la (b) generalizada) si aparece alguna de estas
condiciones:

1. Un daemon con **estado caro de rearmar** o con dependencias externas vivas
   en el andamiaje del loop (p. ej. una conexión persistente abierta fuera
   del `try`), donde un fallo transitorio sí podría escapar la capa 1.
2. Los daemons pasan a ser críticos para **operaciones largas desatendidas**
   (p. ej. pipeline nocturno de rituales) donde disponibilidad pese más que
   visibilidad del fallo.
3. Evidencia empírica (issues/telemetría) de crashes del supervisor causados
   por escapes que un restart habría absorbido de forma segura.

<!--
Corto a propósito. Cada pregunta acá salió de un defecto real que se coló a
producción y lo atajó un bot: si no te aplica, borrá la línea y seguí.
-->

## Qué cambia y por qué

<!-- Una o dos frases. El "por qué" importa más que el "qué". -->

## Hermanos

La clase de defecto #1 del repo (13 de 21 follow-ups auditados) es un fix que
aterriza en un camino y deja intacto al gemelo. Antes de abrir:

- [ ] **Enumeré** los otros caminos que llegan al mismo recurso — no los di por
      descartados de memoria. Los dos ejes que más se escapan:
      - GUI (`SupervisorAgent` → `tool_dispatcher`) **vs.** agente LLM
        (`AsyncToolRegistry` → `LLMRouter` → Telegram / `/api/chat`)
      - funciones hermanas en el mismo archivo (`run_ritual` /
        `run_ritual_install` fue #373)
- [ ] Si excluí alguno, lo digo acá abajo **con su nombre**. Escribir el racional
      de la exclusión no es verificarla: en #318 y #373 el revisor lo revirtió.

Caminos alcanzados / excluidos:

## Verificación

- [ ] `ruff check sky_claw/ tests/` **y** `ruff format --check sky_claw/ tests/`
- [ ] `pytest` con **exit code 0** — no alcanza leer "N passed": el guard de
      `pytest_unconfigure` puede salir con `os._exit(3)` por hilos no-daemon
      justo después de imprimir el resumen (pasó en #362)
- [ ] Si toqué un archivo bajo `sky_claw/app/**` o `sky_claw/local/tools/**`:
      sé que **mypy y BLE001 están exentos ahí** y no me apoyé en esos gates
- [ ] El código nuevo tiene al menos un **call site de producción**, no solo
      tests que lo inyectan (en #240, #252 y #362 la garantía era un no-op fuera
      de la suite, con todo en verde)

## Ancla

- [ ] Si esto cierra una *clase* de defecto, dejé un test que **enumera** la
      familia, no un caso escrito a mano para el hermano que faltaba
      (ver `RITUAL_TOOL_MAP` en `tests/test_ritual_dispatch.py`)

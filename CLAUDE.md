# CLAUDE.md — guía para agentes en este repo

Notas operativas y deuda técnica conocida para quien retome el trabajo (humano o agente).

## Convenciones
- **Tests y comentarios de código en español** (convención del repo).
- **TDD**: test que falla (rojo) → implementación → verde.
- Entorno de tests: venv del repo vía `uv sync --extra dev`; correr con `.venv/bin/python -m pytest`.
  Lint/format/types: `.venv/bin/ruff format --check .`, `.venv/bin/ruff check .`, `.venv/bin/mypy <archivos>`.
  (`asyncio_mode=auto`: los tests `async def` no necesitan decorador.)
- Una rama + un PR por cambio; no commitear directo a `main`.

## Pendientes conocidos / deuda técnica

### #5 — Contrato de resultado compartido de los tools (causa raíz del "error desconocido")
**Problema.** Cada servicio/tool reporta los errores bajo claves distintas:
- LOOT / Pandora / QuickAutoClean → `logs`
- DynDOLOD / LOOT → `errors` (lista)
- xEdit-patch → `error` / `details`
- runners → a veces `stderr`

Por eso `summarize_ritual_result` (`sky_claw/antigravity/gui/controllers/ritual_runner.py`) tiene
que **adivinar** leyendo todas esas claves en cadena; cada vez que aparece una forma nueva,
reaparece el toast opaco **"El ritual «X» falló: error desconocido"**. Ya se parchó dos veces
(PR #214 agregó `logs`/`stderr`; PR #216 agregó la lista `errors`) — son parches, no la solución.

**Propuesta.** Introducir un **contrato de resultado compartido** (`ToolResult` TypedDict o
dataclass: `success: bool`, `message: str`, + campos estructurados como `return_code`, `details`),
o un normalizador único, y que los servicios lo devuelvan / pasen por él. Así el summarizer deja
de adivinar y el bug no vuelve a aparecer.

**Alcance (transversal).** Toca los servicios de tools y sus tests:
`sky_claw/local/tools/{loot_service,pandora_service,dyndolod_service,synthesis_service}.py`,
`sky_claw/local/tools/xedit_service.py` (`quick_auto_clean` / `execute_patch`), y el consumidor
`summarize_ritual_result`. PR dedicado, con tests por servicio. No urgente: los síntomas ya
están parchados.

**Contexto.** Surgió del code-review propio post-Fase 2 (Panel del Draconato). Los otros 4
hallazgos de esa revisión ya están en `main`: flag real de QuickAutoClean `-quickautoclean`
(#216), feedback de DynDOLOD (#216), BodySlide serializado en lock (#217), y la decisión
documentada de que la capa del agente LLM es **lock-only, sin HITL** (#217).

### Verificación pendiente (no es código)
**Smoke real de "Limpiar Archivos" (QuickAutoClean).** Los tests mockean el subproceso, así que
prueban los argumentos pero no que SSEEdit limpie de verdad. Los args ahora coinciden con el
auto-cleaner de referencia (PACT: `-quickautoclean -autoexit -autoload`), pero conviene un smoke
del Ritual en una instalación real con SSEEdit antes de confiar al 100%.

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

### #5 — Contrato de resultado compartido de los tools — ✅ RESUELTO
**Contrato vigente.** Los servicios de tools emiten `success: bool` + `message: str` (canónico,
vacío en éxito) además de sus campos estructurados. `normalize_tool_result`
(`sky_claw/local/tools/tool_result.py`) es la única pieza que conoce las claves legacy
(`details`/`error`/`logs`/`stderr`/`errors`/`reason`) y el summarizer
(`summarize_ritual_result`) la usa en vez de adivinar — "error desconocido" solo puede
originarse en el fallback del normalizador. **Tools nuevos: emitir `success` + `message`.**
Contrato anclado por tests: `tests/test_tool_result.py` (shapes legacy reales) y
`tests/test_tool_result_contract.py` (retorno de error por servicio).

**Historia.** Cada servicio reportaba errores bajo claves distintas (LOOT/Pandora/QuickAutoClean
→ `logs`; DynDOLOD/LOOT → `errors` lista; xEdit-patch → `error`/`details`; runners → `stderr`) y
el summarizer encadenaba todas — parcheado dos veces (#214, #216) antes de esta solución de raíz.

**Contexto.** Surgió del code-review propio post-Fase 2 (Panel del Draconato). Los otros 4
hallazgos de esa revisión ya están en `main`: flag real de QuickAutoClean `-quickautoclean`
(#216), feedback de DynDOLOD (#216), BodySlide serializado en lock (#217), y la decisión
documentada de que la capa del agente LLM es **lock-only, sin HITL** (#217).

### Verificación pendiente (no es código)
**Smoke real de "Limpiar Archivos" (QuickAutoClean).** Los tests mockean el subproceso, así que
prueban los argumentos pero no que SSEEdit limpie de verdad. Los args ahora coinciden con el
auto-cleaner de referencia (PACT: `-quickautoclean -autoexit -autoload`), pero conviene un smoke
del Ritual en una instalación real con SSEEdit antes de confiar al 100%.

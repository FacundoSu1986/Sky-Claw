# Plan de Refactorización de Jerarquía de Directorios — Sky-Claw

> **Auditoría base:** `manuss.txt`  
> **Fecha:** 2026-05-01  
> **Rol:** Arquitecto de Software Principal — Especialista en Refactorización  
> **Entorno:** Windows 10

---

## <pensamiento>

### Análisis Forense Exhaustivo de la Jerarquía Actual

#### 1. Inventario Completo de la Estructura

La raíz del repositorio presenta la siguiente estructura de primer nivel:

```
e:/Skyclaw_Main_Sync/
├── .github/                    # CI/CD + instrucciones IDE-específicas
├── antigravity/                # ← DUPLICADO FANTASMA (raíz)
│   ├── frontend/               # Frontend estático rico (HTML/JS/CSS)
│   ├── gateway/                # Gateway Node.js para Telegram
│   └── tests/                  # Suite de tests Python (~90 archivos)
├── local/                      # Scripts de utilidad local
├── Skills Python/              # Documentación de optimización Python
└── sky_claw/                   # Paquete Python principal
    ├── __init__.py
    ├── __main__.py
    ├── app_context.py
    ├── config.py
    ├── antigravity/            # ← Implementación activa (SSOT)
    │   ├── agent/              # Lógica del agente (router, providers, tools)
    │   ├── comms/              # Comunicaciones (telegram, ws, frontend_bridge)
    │   ├── core/               # Utilidades core (event_bus, database, schemas)
    │   ├── db/                 # Persistencia (registry, journal, snapshots)
    │   ├── gui/                # UI escritorio NiceGUI + Tkinter
    │   ├── modes/              # Puntos de entrada por modo
    │   ├── orchestrator/       # Orquestación de alto nivel
    │   ├── reasoning/          # Motor de razonamiento
    │   ├── scraper/            # Scraping (Nexus, Reddit)
    │   ├── security/           # Seguridad y guardrails
    │   └── web/                # Servidor web aiohttp + static
    └── local/                  # Módulos de entorno local (fomod, xedit, tools)
```

#### 2. Identificación de Problemas

**P1 — Duplicación `antigravity/` (CRÍTICO):**
- Existen DOS directorios `antigravity`: uno en la raíz y otro anidado en `sky_claw/`.
- `sky_claw/antigravity/` es la implementación activa con `__init__.py`, módulos Python funcionales.
- `antigravity/` (raíz) contiene frontend estático, gateway Node.js y tests — es un residuo de una estructura previa.
- `pyproject.toml` línea 46: `testpaths = ["antigravity/tests"]` apunta al directorio fantasma.
- `pyproject.toml` línea 51: `src = ["sky_claw", "antigravity/tests", "local"]` incluye ambos.

**P2 — Frontend Disperso (ALTO):**
- `antigravity/frontend/` contiene el "Operations Hub" rico (360 líneas HTML, 10 módulos JS, 4 CSS).
- `sky_claw/antigravity/web/static/` contiene un UI web básico (319 líneas HTML, `setup.html`).
- Ambos son frontends web servidos por aiohttp pero en ubicaciones inconexas.
- No existe una relación clara entre ambos; el Operations Hub parece ser la versión completa.

**P3 — Gateway Node.js Aislado (MEDIO):**
- `antigravity/gateway/` contiene un servidor Node.js (`server.js`, `telegram_gateway.js`).
- No hay referencia clara a este gateway desde el código Python.
- Debería estar agrupado con el módulo de comunicaciones (`comms/`).

**P4 — Nombres Genéricos/IA (MEDIO):**
- `Skills Python/` — "Skills" es un término genérico de IA/agentes. Contiene documentación de optimización Python no relacionada con el dominio del proyecto.
- `.github/copilot-instructions.md` — Referencia explícita a GitHub Copilot (IDE).
- `sky_claw/antigravity/gui/utils.py` — Nombre genérico "utils" prohibido por manuss.txt.

**P5 — Colisión de Nombres en GUI (MEDIO):**
- `sky_claw/antigravity/gui/event_bus.py` — EventBus sincrónico para Tkinter (Observer pattern, threading/queue).
- `sky_claw/antigravity/core/event_bus.py` — EventBus asíncrono para el sistema (pub/sub, asyncio, DLQ).
- Mismo nombre, responsabilidades diferentes, genera confusión de navegación.

**P6 — `local/` Ambiguo (BAJO):**
- `local/` en la raíz contiene scripts de utilidad (`first_run.py`, `setup_env.ps1`, etc.).
- `sky_claw/local/` contiene módulos Python del dominio (fomod, xedit, tools).
- Ambos usan "local" pero con propósitos completamente diferentes.

#### 3. Análisis de Dependencias

```
antigravity/tests/ → importa desde sky_claw.antigravity.* (tests del paquete principal)
antigravity/frontend/ → servido por sky_claw/antigravity/web/app.py (presunto)
antigravity/gateway/ → independiente (Node.js), comunica con Telegram API
sky_claw/antigravity/gui/event_bus.py → usado por sky_claw/antigravity/gui/*.py
sky_claw/antigravity/core/event_bus.py → usado por supervisor.py, ws_daemon.py, etc.
```

#### 4. Decisión de Diseño

Se aplica el patrón **Strangler Fig** con las siguientes decisiones:

1. **Consolidar `antigravity/` (raíz)** → migrar todo su contenido a ubicaciones canónicas dentro de `sky_claw/` y `tests/`.
2. **Unificar frontend** → Operations Hub bajo `sky_claw/antigravity/web/static/operations_hub/`.
3. **Agrupar gateway** → Node.js gateway bajo `sky_claw/antigravity/comms/telegram_gateway_node/`.
4. **Normalizar tests** → `tests/` en la raíz (convención Python estándar).
5. **Eliminar términos IA/IDE** → Renombrar `Skills Python/`, `.github/copilot-instructions.md`.
6. **Desambiguar GUI** → Renombrar `gui/event_bus.py` y `gui/utils.py`.

</pensamiento>

---

## 1. Diagnóstico Ejecutivo

| # | Punto de Fricción | Severidad | Impacto |
|---|---|---|---|
| 1 | **Directorio `antigravity/` fantasma en raíz** — duplica `sky_claw/antigravity/`, viola SSOT | CRÍTICO | Navegación ambigua, imports confusos, `pyproject.toml` apunta a ubicación no-canónica |
| 2 | **Frontend web disperso** — Operations Hub en `antigravity/frontend/` vs UI básica en `sky_claw/antigravity/web/static/` | ALTO | Dos bases de código frontend sin relación jerárquica, posible duplicación de lógica JS |
| 3 | **Gateway Node.js huérfano** — `antigravity/gateway/` sin integración visible con el paquete Python | MEDIO | Componente de comunicación fuera del módulo `comms/`, difícil de descubrir |
| 4 | **Términos IA/IDE en nomenclatura** — `Skills Python/`, `.github/copilot-instructions.md` | MEDIO | Contaminación semántica, acoplamiento a herramientas específicas |
| 5 | **Colisión `event_bus.py`** — `gui/event_bus.py` (sync/Tkinter) vs `core/event_bus.py` (async/DLQ) | MEDIO | Navegación confusa, riesgo de import incorrecto |
| 6 | **Nombre genérico `utils.py`** en GUI — viola convención de alta densidad informacional | BAJO | Baja descubribilidad del propósito del módulo |
| 7 | **`local/` ambiguo** — scripts raíz vs módulos Python `sky_claw/local/` | BAJO | Dos significados de "local" en el mismo repositorio |

---

## 2. Matriz de Mapeo

| # | Directorio Original | Nuevo Directorio | Justificación Arquitectónica |
|---|---|---|---|
| 1 | `antigravity/` (raíz, completo) | **MIGRAR + ELIMINAR** | Directorio fantasma que duplica `sky_claw/antigravity/`. Viola SSOT. Todo su contenido se redistribuye en los mapeos 2-4. |
| 2 | `antigravity/frontend/` | `sky_claw/antigravity/web/static/operations_hub/` | Consolida el frontend rico (Operations Hub) bajo el módulo `web/` existente, separándolo del UI básico (`index.html`, `setup.html`). |
| 3 | `antigravity/gateway/` | `sky_claw/antigravity/comms/telegram_gateway_node/` | Agrupa el gateway Node.js con el módulo de comunicaciones (`comms/`). El sufijo `_node` indica el runtime. |
| 4 | `antigravity/tests/` | `tests/` (raíz) | Convención Python estándar: tests fuera del paquete en la raíz del repositorio. Actualiza `pyproject.toml`. |
| 5 | `local/` (raíz) | `local_scripts/` | Nombre explícito que distingue los scripts de utilidad local de los módulos Python en `sky_claw/local/`. |
| 6 | `Skills Python/` | `local_docs/python_optimization/` | Elimina el término "Skills" (referencia IA). Nombre descriptivo del contenido real: documentación de optimización Python. |
| 7 | `.github/copilot-instructions.md` | `.github/coding_conventions.md` | Elimina referencia a IDE específico (Copilot). Mantiene el contenido como convenciones de codificación del proyecto. |
| 8 | `sky_claw/antigravity/gui/event_bus.py` | `sky_claw/antigravity/gui/gui_event_adapter.py` | Desambigua del EventBus core (`core/event_bus.py`). El nuevo nombre refleja su rol: adaptador de eventos sincrónico para la GUI. |
| 9 | `sky_claw/antigravity/gui/utils.py` | `sky_claw/antigravity/gui/gui_helpers.py` | Elimina el nombre genérico "utils". El nuevo nombre indica helper functions específicos de la capa GUI. |

---

## 3. Árbol Propuesto

```
e:/Skyclaw_Main_Sync/
│
├── .github/
│   ├── coding_conventions.md          ← era: copilot-instructions.md
│   └── workflows/
│       └── ci.yml
│
├── local_docs/                        ← era: Skills Python/
│   └── python_optimization/
│       ├── SKILL.md
│       ├── advanced-optimization.md
│       └── profiling-tools.md
│
├── local_scripts/                     ← era: local/
│   └── scripts/
│       ├── first_run.py
│       ├── restart_agent.ps1
│       ├── setup_env.ps1
│       └── watcher_daemon.ps1
│
├── tests/                             ← era: antigravity/tests/
│   ├── __init__.py
│   ├── agent/
│   │   ├── __init__.py
│   │   └── test_tool_validation.py
│   ├── fixtures/
│   │   └── fomod/
│   │       ├── complex.xml
│   │       ├── conditional.xml
│   │       └── simple.xml
│   ├── frontend/
│   │   ├── appstate_smoke.mjs
│   │   ├── log_view_smoke.mjs
│   │   └── phase5_binders_smoke.mjs
│   ├── security/
│   │   ├── __init__.py
│   │   └── test_guardrail_bypass.py
│   ├── polling_utils.py
│   ├── test_agent_guardrail.py
│   ├── test_agent_tools.py
│   ├── ... (todos los test_*.py existentes)
│   └── test_xedit_service.py
│
├── sky_claw/                          # Paquete Python principal (SSOT)
│   ├── __init__.py
│   ├── __main__.py
│   ├── app_context.py
│   ├── config.py
│   │
│   ├── antigravity/                   # Núcleo del agente
│   │   ├── __init__.py
│   │   │
│   │   ├── agent/                     # Lógica central del agente
│   │   │   ├── __init__.py
│   │   │   ├── animation_hub.py
│   │   │   ├── context_manager.py
│   │   │   ├── executor.py
│   │   │   ├── hermes_parser.py
│   │   │   ├── providers.py
│   │   │   ├── purple_security_agent.py
│   │   │   ├── router.py
│   │   │   ├── semantic_router.py
│   │   │   ├── token_budget.py
│   │   │   ├── token_circuit_breaker.py
│   │   │   ├── tools_facade.py
│   │   │   └── tools/
│   │   │       ├── __init__.py
│   │   │       ├── db_tools.py
│   │   │       ├── descriptor.py
│   │   │       ├── external_tools.py
│   │   │       ├── nexus_tools.py
│   │   │       ├── schemas.py
│   │   │       └── system_tools.py
│   │   │
│   │   ├── comms/                     # Comunicaciones externas
│   │   │   ├── __init__.py
│   │   │   ├── _transport.py
│   │   │   ├── frontend_bridge.py
│   │   │   ├── interface.py
│   │   │   ├── telegram_polling.py
│   │   │   ├── telegram_sender.py
│   │   │   ├── telegram.py
│   │   │   ├── ws_daemon.py
│   │   │   └── telegram_gateway_node/  ← era: antigravity/gateway/
│   │   │       ├── package.json
│   │   │       ├── server.js
│   │   │       ├── telegram_gateway.js
│   │   │       └── tests/
│   │   │           └── test_timing_safe_equal.js
│   │   │
│   │   ├── core/                      # Utilidades y modelos agnósticos
│   │   │   ├── __init__.py
│   │   │   ├── async_path_resolver.py
│   │   │   ├── contracts.py
│   │   │   ├── database.py
│   │   │   ├── db_lifecycle.py
│   │   │   ├── dlq_manager.py
│   │   │   ├── errors.py
│   │   │   ├── event_bus.py
│   │   │   ├── event_payloads.py
│   │   │   ├── models.py
│   │   │   ├── path_resolver.py
│   │   │   ├── schemas.py
│   │   │   ├── vfs_orchestrator.py
│   │   │   ├── windows_interop.py
│   │   │   └── validators/
│   │   │       ├── __init__.py
│   │   │       ├── path.py
│   │   │       └── ssrf.py
│   │   │
│   │   ├── db/                        # Persistencia de datos
│   │   │   ├── __init__.py
│   │   │   ├── async_registry.py
│   │   │   ├── journal.py
│   │   │   ├── locks.py
│   │   │   ├── registry.py
│   │   │   ├── rollback_manager.py
│   │   │   └── snapshot_manager.py
│   │   │
│   │   ├── gui/                       # UI escritorio (NiceGUI)
│   │   │   ├── agent_communication.py
│   │   │   ├── app.py
│   │   │   ├── dashboard.py
│   │   │   ├── gui_event_adapter.py   ← era: event_bus.py
│   │   │   ├── gui_helpers.py         ← era: utils.py
│   │   │   ├── icons.py
│   │   │   ├── message_handlers.py
│   │   │   ├── setup_wizard.py
│   │   │   ├── sky_claw_gui.py
│   │   │   ├── styles.css
│   │   │   ├── assets/
│   │   │   │   ├── alduin_menace_bg.jpg
│   │   │   │   ├── parchment.png
│   │   │   │   └── stone_bg.png
│   │   │   ├── controllers/
│   │   │   │   ├── __init__.py
│   │   │   │   ├── chat_controller.py
│   │   │   │   ├── mod_controller.py
│   │   │   │   └── navigation_controller.py
│   │   │   ├── models/
│   │   │   │   ├── __init__.py
│   │   │   │   └── app_state.py
│   │   │   └── views/
│   │   │       ├── __init__.py
│   │   │       ├── actions.py
│   │   │       ├── advanced.py
│   │   │       ├── mod_list.py
│   │   │       ├── components/
│   │   │       │   ├── __init__.py
│   │   │       │   ├── buttons.py
│   │   │       │   ├── chat_bubble.py
│   │   │       │   ├── feature_card.py
│   │   │       │   ├── mod_item.py
│   │   │       │   └── stat_card.py
│   │   │       ├── layout/
│   │   │       │   ├── __init__.py
│   │   │       │   ├── header.py
│   │   │       │   └── sidebar.py
│   │   │       ├── pages/
│   │   │       │   ├── __init__.py
│   │   │       │   └── dashboard_page.py
│   │   │       └── sections/
│   │   │           ├── __init__.py
│   │   │           ├── chat_preview.py
│   │   │           ├── cta_section.py
│   │   │           ├── features_section.py
│   │   │           ├── mods_preview.py
│   │   │           └── stats_section.py
│   │   │
│   │   ├── modes/                     # Puntos de entrada por modo
│   │   │   ├── __init__.py
│   │   │   ├── cli_mode.py
│   │   │   ├── gui_mode.py
│   │   │   ├── security_mode.py
│   │   │   ├── telegram_mode.py
│   │   │   └── web_mode.py
│   │   │
│   │   ├── orchestrator/              # Orquestación de alto nivel
│   │   │   ├── __init__.py
│   │   │   ├── maintenance_daemon.py
│   │   │   ├── rollback_factory.py
│   │   │   ├── state_graph.py
│   │   │   ├── supervisor.py
│   │   │   ├── sync_engine.py
│   │   │   ├── telemetry_daemon.py
│   │   │   ├── tool_dispatcher.py
│   │   │   ├── tool_state_machine.py
│   │   │   ├── watcher_daemon.py
│   │   │   ├── ws_event_streamer.py
│   │   │   └── tool_strategies/
│   │   │       ├── __init__.py
│   │   │       ├── base.py
│   │   │       ├── execute_loot_sorting.py
│   │   │       ├── execute_synthesis.py
│   │   │       ├── generate_bashed_patch.py
│   │   │       ├── generate_lods.py
│   │   │       ├── middleware.py
│   │   │       ├── query_mod_metadata.py
│   │   │       ├── resolve_conflict_patch.py
│   │   │       ├── scan_asset_conflicts.py
│   │   │       └── validate_plugin_limit.py
│   │   │
│   │   ├── reasoning/                 # Motor de razonamiento
│   │   │   ├── __init__.py
│   │   │   ├── engine.py
│   │   │   ├── strategies.py
│   │   │   ├── tot.py
│   │   │   └── types.py
│   │   │
│   │   ├── scraper/                   # Scraping web
│   │   │   ├── __init__.py
│   │   │   ├── masterlist.py
│   │   │   ├── nexus_downloader.py
│   │   │   ├── nexus.py
│   │   │   ├── reddit_client.py
│   │   │   └── scraper_agent.py
│   │   │
│   │   ├── security/                  # Seguridad y guardrails
│   │   │   ├── __init__.py
│   │   │   ├── agent_guardrail.py
│   │   │   ├── auth_token_manager.py
│   │   │   ├── credential_vault.py
│   │   │   ├── file_permissions.py
│   │   │   ├── governance.py
│   │   │   ├── hitl.py
│   │   │   ├── loop_guardrail.py
│   │   │   ├── metacognitive_logic.py
│   │   │   ├── network_gateway.py
│   │   │   ├── path_validator.py
│   │   │   ├── prompt_armor.py
│   │   │   ├── purple_scanner.py
│   │   │   ├── sanitize.py
│   │   │   └── text_inspector.py
│   │   │
│   │   └── web/                       # Servidor web aiohttp
│   │       ├── __init__.py
│   │       ├── app.py
│   │       ├── operations_hub_ws.py
│   │       └── static/
│   │           ├── index.html         # UI web básica
│   │           ├── setup.html
│   │           └── operations_hub/    ← era: antigravity/frontend/
│   │               ├── index.html
│   │               ├── Sky-Claw Operations Hub.html
│   │               ├── css/
│   │               │   ├── dark-glass.css
│   │               │   ├── medieval-theme.css
│   │               │   ├── operations-hub.css
│   │               │   └── sky_claw_styles.css
│   │               └── js/
│   │                   ├── app.js
│   │                   ├── appstate.js
│   │                   ├── arsenal.js
│   │                   ├── dashboard.js
│   │                   ├── log-view.js
│   │                   ├── operations-hub.js
│   │                   ├── panel-collapse.js
│   │                   ├── status-bar.js
│   │                   ├── telemetry.js
│   │                   └── websocket-client.js
│   │
│   └── local/                         # Módulos de entorno local
│       ├── __init__.py
│       ├── auto_detect.py
│       ├── local_config.py
│       ├── tools_installer.py
│       ├── assets/
│       ├── discovery/
│       ├── fomod/
│       ├── loot/
│       ├── mo2/
│       ├── tools/
│       ├── validators/
│       └── xedit/
│
├── .gitattributes
├── .gitignore
├── .pre-commit-config.yaml
├── build.bat
├── LICENSE
├── manuss.txt
├── pyproject.toml                     # ← Actualizar testpaths y src
├── QUICKSTART.md
├── README.md
├── requirements.lock
├── SECURITY.md
└── sky_claw.spec
```

---

## 4. Actualizaciones Requeridas en `pyproject.toml`

```toml
# ANTES:
[tool.pytest.ini_options]
testpaths = ["antigravity/tests"]

[tool.ruff]
src = ["sky_claw", "antigravity/tests", "local"]

# DESPUÉS:
[tool.pytest.ini_options]
testpaths = ["tests"]

[tool.ruff]
src = ["sky_claw", "tests", "local_scripts"]
```

---

## 5. Actualizaciones Requeridas de Imports

Los siguientes archivos requieren actualización de imports internos tras el renombramiento:

| Archivo | Cambio de Import |
|---|---|
| `sky_claw/antigravity/gui/*.py` (todos los que importen `event_bus`) | `from sky_claw.antigravity.gui.event_bus` → `from sky_claw.antigravity.gui.gui_event_adapter` |
| `sky_claw/antigravity/gui/*.py` (todos los que importen `utils`) | `from sky_claw.antigravity.gui.utils` → `from sky_claw.antigravity.gui.gui_helpers` |
| `sky_claw/antigravity/web/app.py` | Actualizar rutas estáticas para servir `static/operations_hub/` |
| `tests/**/*.py` | Actualizar imports si usan rutas relativas a `antigravity/tests/` |

---

*Fin del documento de planificación. Ejecutar con `refactor_execute.ps1`.*

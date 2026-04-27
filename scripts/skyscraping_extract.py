#!/usr/bin/env python3
"""
Sky Claw Sync - Extracción Modular de Código Fuente
Genera la carpeta skyscraping/ con archivos .txt organizados por funcionalidad arquitectónica.
"""

import logging
import sys
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# Base directory is the project root
BASE_DIR = Path(__file__).resolve().parent.parent
OUTPUT_DIR = BASE_DIR / "skyscraping"
EXTRACTION_DATE = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")

# Language detection
LANG_MAP = {
    ".py": "Python",
    ".js": "JavaScript",
    ".html": "HTML",
    ".css": "CSS",
    ".json": "JSON",
    ".toml": "TOML",
    ".txt": "Text",
    ".bat": "Batch (Windows)",
    ".spec": "PyInstaller Spec",
    ".ps1": "PowerShell",
    ".pas": "Pascal (xEdit Script)",
    ".lock": "Lock file",
    ".mjs": "JavaScript (ES Module)",
    ".md": "Markdown",
}


def get_lang(ext: str) -> str:
    return LANG_MAP.get(ext, f"Unknown ({ext})")


def read_file_safe(filepath: Path) -> str | None:
    """Read file content with multiple encoding attempts."""
    encodings = ["utf-8", "utf-8-sig", "latin-1", "cp1252", "ascii"]
    for enc in encodings:
        try:
            return filepath.read_text(encoding=enc)
        except (UnicodeDecodeError, UnicodeError):
            continue
        except OSError as e:
            logger.warning("Cannot read %s: %s", filepath, e)
            return None
    logger.warning("Could not decode %s with any encoding", filepath)
    return None


def format_file_entry(relative_path: str, content: str, description: str = "") -> str:
    """Format a single file entry with headers."""
    ext = Path(relative_path).suffix
    lang = get_lang(ext)
    desc = f"\n// Descripción: {description}" if description else ""

    header = (
        f"//----------------------------------------------------------------------------//\n"
        f"// ARCHIVO: {relative_path}\n"
        f"// Lenguaje: {lang}{desc}\n"
        f"//----------------------------------------------------------------------------//\n\n"
    )

    footer = (
        f"\n\n//----------------------------------------------------------------------------//\n"
        f"// FIN DE ARCHIVO: {relative_path}\n"
        f"//----------------------------------------------------------------------------//\n"
    )

    return header + content + footer


def write_module_file(output_path: Path, module_name: str, entries: list[str]):
    """Write a complete module .txt file with all entries."""
    separator = "=" * 80
    module_header = (
        f"{separator}\n"
        f"SKY CLAW SYNC - Extracción de Código Fuente\n"
        f"Módulo: {module_name}\n"
        f"Archivo: {output_path.name}\n"
        f"Fecha de extracción: {EXTRACTION_DATE}\n"
        f"Total de archivos en este módulo: {len(entries)}\n"
        f"{separator}\n\n"
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(module_header)
        for i, entry in enumerate(entries):
            f.write(entry)
            if i < len(entries) - 1:
                f.write("\n\n")


# =============================================================================
# MODULE DEFINITIONS
# Each module maps an output .txt path to a list of (source_relative_path, description) tuples
# =============================================================================

MODULES = {
    # ── 01 FRONTEND ──────────────────────────────────────────────────────────
    "01_frontend/frontend_html.txt": {
        "name": "Frontend - HTML",
        "files": [
            ("frontend/index.html", "Página principal del frontend web"),
            ("frontend/Sky-Claw Operations Hub.html", "Hub de operaciones - interfaz web avanzada"),
            ("sky_claw/web/static/index.html", "Página estática del servidor web"),
            ("sky_claw/web/static/setup.html", "Página de configuración inicial"),
        ],
    },
    "01_frontend/frontend_css.txt": {
        "name": "Frontend - CSS (Estilos)",
        "files": [
            ("frontend/css/dark-glass.css", "Tema Dark Glass - interfaz oscura con efecto cristal"),
            ("frontend/css/medieval-theme.css", "Tema Medieval - estética fantástica medieval"),
            ("frontend/css/operations-hub.css", "Estilos del Hub de Operaciones"),
            ("frontend/css/sky_claw_styles.css", "Estilos generales de Sky Claw"),
            ("sky_claw/gui/styles.css", "Estilos CSS de la GUI desktop"),
        ],
    },
    "01_frontend/frontend_js.txt": {
        "name": "Frontend - JavaScript",
        "files": [
            ("frontend/js/app.js", "Aplicación principal frontend"),
            ("frontend/js/appstate.js", "Gestión de estado de la aplicación"),
            ("frontend/js/arsenal.js", "Módulo Arsenal - herramientas disponibles"),
            ("frontend/js/dashboard.js", "Dashboard - panel de control principal"),
            ("frontend/js/log-view.js", "Visor de logs en tiempo real"),
            ("frontend/js/operations-hub.js", "Controlador del Hub de Operaciones"),
            ("frontend/js/panel-collapse.js", "Colapso/expansión de paneles UI"),
            ("frontend/js/status-bar.js", "Barra de estado del frontend"),
            ("frontend/js/telemetry.js", "Telemetría - métricas y monitoreo"),
            ("frontend/js/websocket-client.js", "Cliente WebSocket para comunicación en tiempo real"),
        ],
    },
    # ── 02 BACKEND CORE ──────────────────────────────────────────────────────
    "02_backend_core/core_init_main.txt": {
        "name": "Backend Core - Inicialización y Entry Points",
        "files": [
            ("sky_claw/__init__.py", "Inicialización del paquete sky_claw"),
            ("sky_claw/__main__.py", "Entry point principal del módulo sky_claw"),
            ("sky_claw/app_context.py", "Contexto global de la aplicación"),
            ("sky_claw/auto_detect.py", "Auto-detección de entorno y herramientas"),
        ],
    },
    "02_backend_core/core_models_schemas.txt": {
        "name": "Backend Core - Modelos y Schemas",
        "files": [
            ("sky_claw/core/models.py", "Modelos de datos centrales"),
            ("sky_claw/core/schemas.py", "Esquemas de validación y serialización"),
            ("sky_claw/core/event_payloads.py", "Definición de payloads de eventos"),
        ],
    },
    "02_backend_core/core_database.txt": {
        "name": "Backend Core - Base de Datos Central",
        "files": [
            ("sky_claw/core/database.py", "Capa central de base de datos"),
        ],
    },
    "02_backend_core/core_event_bus.txt": {
        "name": "Backend Core - Bus de Eventos",
        "files": [
            ("sky_claw/core/event_bus.py", "Sistema de bus de eventos pub/sub"),
        ],
    },
    "02_backend_core/core_errors_contracts.txt": {
        "name": "Backend Core - Errores y Contratos",
        "files": [
            ("sky_claw/core/errors.py", "Jerarquía de errores y excepciones custom"),
            ("sky_claw/core/contracts.py", "Contratos y pre/post condiciones"),
        ],
    },
    "02_backend_core/core_path_resolver.txt": {
        "name": "Backend Core - Resolución de Rutas",
        "files": [
            ("sky_claw/core/path_resolver.py", "Resolvedor de rutas síncrono"),
            ("sky_claw/core/async_path_resolver.py", "Resolvedor de rutas asíncrono"),
        ],
    },
    "02_backend_core/core_vfs_interop.txt": {
        "name": "Backend Core - VFS e Interoperabilidad Windows",
        "files": [
            ("sky_claw/core/vfs_orchestrator.py", "Orquestador del Virtual File System"),
            ("sky_claw/core/windows_interop.py", "Interoperabilidad con APIs de Windows"),
        ],
    },
    "02_backend_core/core_dlq.txt": {
        "name": "Backend Core - Dead Letter Queue",
        "files": [
            ("sky_claw/core/dlq_manager.py", "Gestor de Dead Letter Queue para mensajes fallidos"),
        ],
    },
    "02_backend_core/core_validators.txt": {
        "name": "Backend Core - Validadores Internos",
        "files": [
            ("sky_claw/core/validators/__init__.py", "Inicialización del módulo de validadores"),
            ("sky_claw/core/validators/path.py", "Validador de rutas de archivos"),
            ("sky_claw/core/validators/ssrf.py", "Protección contra ataques SSRF"),
        ],
    },
    # ── 03 DATABASE ──────────────────────────────────────────────────────────
    "03_database/db_registry.txt": {
        "name": "Base de Datos - Registro de Mods",
        "files": [
            ("sky_claw/db/__init__.py", "Inicialización del módulo db"),
            ("sky_claw/db/registry.py", "Registro síncrono de mods en base de datos"),
            ("sky_claw/db/async_registry.py", "Registro asíncrono de mods en base de datos"),
        ],
    },
    "03_database/db_journal_locks.txt": {
        "name": "Base de Datos - Journaling y Locks",
        "files": [
            ("sky_claw/db/journal.py", "Sistema de journaling para operaciones atómicas"),
            ("sky_claw/db/locks.py", "Locks distribuidos para concurrencia"),
        ],
    },
    "03_database/db_rollback_snapshot.txt": {
        "name": "Base de Datos - Rollback y Snapshots",
        "files": [
            ("sky_claw/db/rollback_manager.py", "Gestor de rollback para operaciones fallidas"),
            ("sky_claw/db/snapshot_manager.py", "Sistema de snapshots para backup/restore"),
        ],
    },
    # ── 04 AGENT AI ──────────────────────────────────────────────────────────
    "04_agent_ai/agent_core.txt": {
        "name": "Agente AI - Núcleo",
        "files": [
            ("sky_claw/agent/__init__.py", "Inicialización del módulo agent"),
            ("sky_claw/agent/executor.py", "Ejecutor principal del agente AI"),
            ("sky_claw/agent/router.py", "Router de intenciones del agente"),
            ("sky_claw/agent/semantic_router.py", "Router semántico basado en embeddings"),
            ("sky_claw/agent/providers.py", "Proveedores de LLM (OpenAI, Gemini, etc.)"),
        ],
    },
    "04_agent_ai/agent_integration.txt": {
        "name": "Agente AI - Integraciones",
        "files": [
            ("sky_claw/agent/autogen_integration.py", "Integración con AutoGen multi-agente"),
            ("sky_claw/agent/context_manager.py", "Gestor de contexto y ventana de tokens"),
            ("sky_claw/agent/lcel_chains.py", "Cadenas LCEL (LangChain Expression Language)"),
        ],
    },
    "04_agent_ai/agent_bridges.txt": {
        "name": "Agente AI - Bridges y Herramientas",
        "files": [
            ("sky_claw/agent/specialized_bridges.py", "Bridges especializados para herramientas"),
            ("sky_claw/agent/hermes_parser.py", "Parser Hermes para respuestas del agente"),
            ("sky_claw/agent/tools_facade.py", "Fachada de herramientas del agente"),
        ],
    },
    "04_agent_ai/agent_security_animation.txt": {
        "name": "Agente AI - Seguridad y Animación",
        "files": [
            ("sky_claw/agent/purple_security_agent.py", "Agente de seguridad Purple Team"),
            ("sky_claw/agent/animation_hub.py", "Hub de animaciones para feedback visual"),
        ],
    },
    "04_agent_ai/agent_tools.txt": {
        "name": "Agente AI - Tools del Agente",
        "files": [
            ("sky_claw/agent/tools/__init__.py", "Inicialización de tools del agente"),
            ("sky_claw/agent/tools/db_tools.py", "Herramientas de base de datos del agente"),
            ("sky_claw/agent/tools/descriptor.py", "Descriptor de herramientas disponibles"),
            ("sky_claw/agent/tools/external_tools.py", "Herramientas externas del agente"),
            ("sky_claw/agent/tools/nexus_tools.py", "Herramientas de integración con Nexus Mods"),
            ("sky_claw/agent/tools/schemas.py", "Schemas de las herramientas del agente"),
            ("sky_claw/agent/tools/system_tools.py", "Herramientas de sistema del agente"),
        ],
    },
    "04_agent_ai/reasoning.txt": {
        "name": "Agente AI - Razonamiento",
        "files": [
            ("sky_claw/reasoning/__init__.py", "Inicialización del módulo reasoning"),
            ("sky_claw/reasoning/engine.py", "Motor de razonamiento del agente"),
            ("sky_claw/reasoning/strategies.py", "Estrategias de razonamiento"),
            ("sky_claw/reasoning/tot.py", "Tree of Thought - razonamiento ramificado"),
            ("sky_claw/reasoning/types.py", "Tipos de datos para el sistema de razonamiento"),
        ],
    },
    # ── 05 ORCHESTRATOR ──────────────────────────────────────────────────────
    "05_orchestrator/orchestrator_core.txt": {
        "name": "Orquestador - Núcleo",
        "files": [
            ("sky_claw/orchestrator/__init__.py", "Inicialización del módulo orchestrator"),
            ("sky_claw/orchestrator/supervisor.py", "Supervisor principal - orquestación de alto nivel"),
            ("sky_claw/orchestrator/sync_engine.py", "Motor de sincronización de mods"),
            ("sky_claw/orchestrator/state_graph.py", "Grafo de estados - flujo de trabajo con LangGraph"),
        ],
    },
    "05_orchestrator/orchestrator_daemons.txt": {
        "name": "Orquestador - Daemons",
        "files": [
            ("sky_claw/orchestrator/maintenance_daemon.py", "Daemon de mantenimiento periódico"),
            ("sky_claw/orchestrator/telemetry_daemon.py", "Daemon de telemetría y métricas"),
            ("sky_claw/orchestrator/watcher_daemon.py", "Daemon vigilante de cambios en archivos"),
        ],
    },
    "05_orchestrator/orchestrator_dispatcher.txt": {
        "name": "Orquestador - Dispatcher y Event Streamer",
        "files": [
            ("sky_claw/orchestrator/tool_dispatcher.py", "Dispatcher de herramientas del orquestador"),
            ("sky_claw/orchestrator/ws_event_streamer.py", "Streamer de eventos vía WebSocket"),
        ],
    },
    "05_orchestrator/tool_strategies.txt": {
        "name": "Orquestador - Estrategias de Herramientas",
        "files": [
            ("sky_claw/orchestrator/tool_strategies/__init__.py", "Inicialización de tool_strategies"),
            ("sky_claw/orchestrator/tool_strategies/base.py", "Clase base para estrategias de herramientas"),
            ("sky_claw/orchestrator/tool_strategies/execute_loot_sorting.py", "Estrategia: ordenamiento LOOT"),
            ("sky_claw/orchestrator/tool_strategies/execute_synthesis.py", "Estrategia: ejecución Synthesis"),
            ("sky_claw/orchestrator/tool_strategies/generate_bashed_patch.py", "Estrategia: generación Bashed Patch"),
            ("sky_claw/orchestrator/tool_strategies/generate_lods.py", "Estrategia: generación de LODs"),
            ("sky_claw/orchestrator/tool_strategies/middleware.py", "Middleware para estrategias"),
            ("sky_claw/orchestrator/tool_strategies/query_mod_metadata.py", "Estrategia: consulta metadatos de mods"),
            ("sky_claw/orchestrator/tool_strategies/resolve_conflict_patch.py", "Estrategia: resolución de conflictos"),
            (
                "sky_claw/orchestrator/tool_strategies/scan_asset_conflicts.py",
                "Estrategia: escaneo de conflictos de assets",
            ),
            (
                "sky_claw/orchestrator/tool_strategies/validate_plugin_limit.py",
                "Estrategia: validación de límite de plugins",
            ),
        ],
    },
    # ── 06 API CONTROLLERS ───────────────────────────────────────────────────
    "06_api_controllers/web_app.txt": {
        "name": "APIs y Controladores - Servidor Web",
        "files": [
            ("sky_claw/web/__init__.py", "Inicialización del módulo web"),
            ("sky_claw/web/app.py", "Aplicación web principal (FastAPI/Starlette)"),
            ("sky_claw/web/operations_hub_ws.py", "WebSocket del Hub de Operaciones"),
        ],
    },
    "06_api_controllers/gui_controllers.txt": {
        "name": "APIs y Controladores - Controladores GUI",
        "files": [
            ("sky_claw/gui/controllers/__init__.py", "Inicialización de controladores GUI"),
            ("sky_claw/gui/controllers/chat_controller.py", "Controlador del chat con el agente"),
            ("sky_claw/gui/controllers/mod_controller.py", "Controlador de gestión de mods"),
            ("sky_claw/gui/controllers/navigation_controller.py", "Controlador de navegación entre vistas"),
        ],
    },
    "06_api_controllers/gateway.txt": {
        "name": "APIs y Controladores - Gateway (Node.js)",
        "files": [
            ("gateway/package.json", "Configuración del paquete Node.js del gateway"),
            ("gateway/server.js", "Servidor gateway principal"),
            ("gateway/telegram_gateway.js", "Gateway de Telegram - puente con bot"),
        ],
    },
    # ── 07 COMMUNICATIONS ────────────────────────────────────────────────────
    "07_communications/telegram.txt": {
        "name": "Comunicaciones - Telegram",
        "files": [
            ("sky_claw/comms/__init__.py", "Inicialización del módulo comms"),
            ("sky_claw/comms/telegram.py", "Cliente principal de Telegram"),
            ("sky_claw/comms/telegram_polling.py", "Polling de mensajes Telegram"),
            ("sky_claw/comms/telegram_sender.py", "Envío de mensajes Telegram"),
        ],
    },
    "07_communications/websocket.txt": {
        "name": "Comunicaciones - WebSocket",
        "files": [
            ("sky_claw/comms/ws_daemon.py", "Daemon WebSocket para comunicación en tiempo real"),
        ],
    },
    "07_communications/frontend_bridge.txt": {
        "name": "Comunicaciones - Bridge Frontend",
        "files": [
            ("sky_claw/comms/frontend_bridge.py", "Puente de comunicación con el frontend"),
            ("sky_claw/comms/interface.py", "Interfaz de comunicación abstracta"),
        ],
    },
    # ── 08 SECURITY ──────────────────────────────────────────────────────────
    "08_security/security_governance.txt": {
        "name": "Seguridad - Gobernanza y HITL",
        "files": [
            ("sky_claw/security/__init__.py", "Inicialización del módulo security"),
            ("sky_claw/security/governance.py", "Gobernanza de seguridad - políticas y compliance"),
            ("sky_claw/security/hitl.py", "Human-in-the-Loop - aprobación humana para operaciones críticas"),
        ],
    },
    "08_security/security_guardrails.txt": {
        "name": "Seguridad - Guardrails",
        "files": [
            ("sky_claw/security/agent_guardrail.py", "Guardrails del agente AI - límites de comportamiento"),
            ("sky_claw/security/loop_guardrail.py", "Protección contra bucles infinitos del agente"),
            ("sky_claw/security/metacognitive_logic.py", "Lógica metacognitiva - auto-evaluación del agente"),
        ],
    },
    "08_security/security_auth.txt": {
        "name": "Seguridad - Autenticación y Credenciales",
        "files": [
            ("sky_claw/security/auth_token_manager.py", "Gestor de tokens de autenticación"),
            ("sky_claw/security/credential_vault.py", "Bóveda de credenciales encriptadas"),
        ],
    },
    "08_security/security_network.txt": {
        "name": "Seguridad - Red y Rutas",
        "files": [
            ("sky_claw/security/network_gateway.py", "Gateway de red - control de tráfico"),
            ("sky_claw/security/path_validator.py", "Validador de rutas - protección path traversal"),
            ("sky_claw/security/file_permissions.py", "Gestor de permisos de archivos"),
        ],
    },
    "08_security/security_scanners.txt": {
        "name": "Seguridad - Scanners y Sanitización",
        "files": [
            ("sky_claw/security/purple_scanner.py", "Scanner Purple Team - análisis de seguridad"),
            ("sky_claw/security/sanitize.py", "Sanitización de inputs y outputs"),
            ("sky_claw/security/text_inspector.py", "Inspector de texto - detección de contenido peligroso"),
        ],
    },
    # ── 09 TOOLS/RUNNERS ─────────────────────────────────────────────────────
    "09_tools_runners/mod_tools.txt": {
        "name": "Tools/Runners - Herramientas de Mods",
        "files": [
            ("sky_claw/tools/__init__.py", "Inicialización del módulo tools"),
            ("sky_claw/tools/bodyslide_runner.py", "Runner de BodySlide - generación de bodies"),
            ("sky_claw/tools/dyndolod_runner.py", "Runner de DynDOLOD - generación de LODs"),
            ("sky_claw/tools/dyndolod_service.py", "Servicio DynDOLOD - lógica de negocio"),
            ("sky_claw/tools/pandora_runner.py", "Runner de Pandora - comportamiento NPC"),
            ("sky_claw/tools/patcher_pipeline.py", "Pipeline de patchers - cadena de parcheado"),
            ("sky_claw/tools/synthesis_runner.py", "Runner de Synthesis - patcher framework"),
            ("sky_claw/tools/synthesis_service.py", "Servicio Synthesis - lógica de negocio"),
            ("sky_claw/tools/vramr_service.py", "Servicio VRAMR - optimización de VRAM"),
            ("sky_claw/tools/wrye_bash_runner.py", "Runner de Wrye Bash - gestión de mods"),
            ("sky_claw/tools/xedit_service.py", "Servicio xEdit - análisis y parcheado"),
        ],
    },
    "09_tools_runners/xedit_core.txt": {
        "name": "Tools/Runners - xEdit Core",
        "files": [
            ("sky_claw/xedit/__init__.py", "Inicialización del módulo xedit"),
            ("sky_claw/xedit/conflict_analyzer.py", "Analizador de conflictos entre plugins"),
            ("sky_claw/xedit/output_parser.py", "Parser de salida de xEdit"),
            ("sky_claw/xedit/patch_orchestrator.py", "Orquestador de parches xEdit"),
            ("sky_claw/xedit/runner.py", "Runner principal de xEdit"),
        ],
    },
    "09_tools_runners/xedit_scripts.txt": {
        "name": "Tools/Runners - Scripts xEdit (Pascal)",
        "files": [
            ("sky_claw/xedit/scripts/apply_leveled_list_merge.pas", "Script Pascal: merge de leveled lists"),
            ("sky_claw/xedit/scripts/list_all_conflicts.pas", "Script Pascal: listar todos los conflictos"),
        ],
    },
    # ── 10 SCRAPER ───────────────────────────────────────────────────────────
    "10_scraper/scraper_all.txt": {
        "name": "Scraper - Descarga y Scraping",
        "files": [
            ("sky_claw/scraper/__init__.py", "Inicialización del módulo scraper"),
            ("sky_claw/scraper/nexus.py", "Scraper de Nexus Mods"),
            ("sky_claw/scraper/nexus_downloader.py", "Descargador de mods desde Nexus"),
            ("sky_claw/scraper/reddit_client.py", "Cliente de Reddit - búsqueda de mods"),
            ("sky_claw/scraper/scraper_agent.py", "Agente de scraping autónomo"),
            ("sky_claw/scraper/masterlist.py", "Masterlist del scraper"),
        ],
    },
    # ── 11 GUI DESKTOP ───────────────────────────────────────────────────────
    "11_gui/gui_main.txt": {
        "name": "GUI Desktop - Aplicación Principal",
        "files": [
            ("sky_claw/gui/app.py", "Aplicación GUI principal (tkinter/customtkinter)"),
            ("sky_claw/gui/sky_claw_gui.py", "Ventana principal de Sky Claw GUI"),
            ("sky_claw/gui/dashboard.py", "Dashboard de la GUI desktop"),
        ],
    },
    "11_gui/gui_communication.txt": {
        "name": "GUI Desktop - Comunicación y Eventos",
        "files": [
            ("sky_claw/gui/agent_communication.py", "Comunicación GUI ↔ Agente AI"),
            ("sky_claw/gui/message_handlers.py", "Handlers de mensajes en la GUI"),
            ("sky_claw/gui/event_bus.py", "Bus de eventos de la GUI"),
            ("sky_claw/gui/setup_wizard.py", "Wizard de configuración inicial"),
        ],
    },
    "11_gui/gui_views.txt": {
        "name": "GUI Desktop - Vistas y Componentes",
        "files": [
            ("sky_claw/gui/views/__init__.py", "Inicialización de vistas"),
            ("sky_claw/gui/views/actions.py", "Vista de acciones"),
            ("sky_claw/gui/views/advanced.py", "Vista avanzada"),
            ("sky_claw/gui/views/mod_list.py", "Vista de lista de mods"),
            ("sky_claw/gui/views/components/__init__.py", "Inicialización de componentes"),
            ("sky_claw/gui/views/components/buttons.py", "Componente: botones custom"),
            ("sky_claw/gui/views/components/chat_bubble.py", "Componente: burbuja de chat"),
            ("sky_claw/gui/views/components/feature_card.py", "Componente: tarjeta de feature"),
            ("sky_claw/gui/views/components/mod_item.py", "Componente: item de mod"),
            ("sky_claw/gui/views/components/stat_card.py", "Componente: tarjeta de estadísticas"),
            ("sky_claw/gui/views/layout/__init__.py", "Inicialización de layout"),
            ("sky_claw/gui/views/layout/header.py", "Layout: encabezado"),
            ("sky_claw/gui/views/layout/sidebar.py", "Layout: barra lateral"),
            ("sky_claw/gui/views/pages/__init__.py", "Inicialización de páginas"),
            ("sky_claw/gui/views/pages/dashboard_page.py", "Página: dashboard"),
            ("sky_claw/gui/views/sections/__init__.py", "Inicialización de secciones"),
            ("sky_claw/gui/views/sections/chat_preview.py", "Sección: preview del chat"),
            ("sky_claw/gui/views/sections/cta_section.py", "Sección: call to action"),
            ("sky_claw/gui/views/sections/features_section.py", "Sección: features"),
            ("sky_claw/gui/views/sections/mods_preview.py", "Sección: preview de mods"),
            ("sky_claw/gui/views/sections/stats_section.py", "Sección: estadísticas"),
        ],
    },
    "11_gui/gui_models.txt": {
        "name": "GUI Desktop - Modelos",
        "files": [
            ("sky_claw/gui/models/__init__.py", "Inicialización de modelos GUI"),
            ("sky_claw/gui/models/app_state.py", "Estado global de la aplicación GUI"),
        ],
    },
    "11_gui/gui_utils.txt": {
        "name": "GUI Desktop - Utilidades",
        "files": [
            ("sky_claw/gui/utils.py", "Funciones utilitarias de la GUI"),
            ("sky_claw/gui/icons.py", "Definición de iconos de la GUI"),
        ],
    },
    # ── 12 CONFIGURACIONES ───────────────────────────────────────────────────
    "12_config/project_config.txt": {
        "name": "Configuraciones - Proyecto",
        "files": [
            ("pyproject.toml", "Configuración del proyecto Python (build system, deps)"),
            ("requirements-local-agents.txt", "Dependencias para agentes locales"),
            ("requirements.lock", "Lock file de dependencias"),
        ],
    },
    "12_config/app_config.txt": {
        "name": "Configuraciones - Aplicación",
        "files": [
            ("sky_claw/config.py", "Configuración principal de la aplicación"),
            ("sky_claw/local_config.py", "Configuración local del usuario"),
            ("sky_claw/logging_config.py", "Configuración del sistema de logging"),
            ("sky_claw/tools_installer.py", "Instalador de herramientas externas"),
        ],
    },
    "12_config/build_config.txt": {
        "name": "Configuraciones - Build",
        "files": [
            ("build.bat", "Script de build para Windows"),
            ("sky_claw.spec", "Spec file de PyInstaller"),
        ],
    },
    # ── 13 MODES ─────────────────────────────────────────────────────────────
    "13_modes/modes_all.txt": {
        "name": "Modos de Ejecución",
        "files": [
            ("sky_claw/modes/__init__.py", "Inicialización del módulo modes"),
            ("sky_claw/modes/cli_mode.py", "Modo CLI - interfaz de línea de comandos"),
            ("sky_claw/modes/gui_mode.py", "Modo GUI - interfaz gráfica desktop"),
            ("sky_claw/modes/security_mode.py", "Modo Seguridad - auditoría y análisis"),
            ("sky_claw/modes/telegram_mode.py", "Modo Telegram - bot de chat"),
            ("sky_claw/modes/web_mode.py", "Modo Web - interfaz web"),
        ],
    },
    # ── 14 DISCOVERY/LOOT/FOMOD/MO2/ASSETS ───────────────────────────────────
    "14_discovery_loot_fomod_mo2/discovery.txt": {
        "name": "Discovery - Detección de Entorno",
        "files": [
            ("sky_claw/discovery/__init__.py", "Inicialización del módulo discovery"),
            ("sky_claw/discovery/environment.py", "Detección del entorno de juego"),
            ("sky_claw/discovery/scanner.py", "Scanner de archivos y directorios"),
        ],
    },
    "14_discovery_loot_fomod_mo2/loot.txt": {
        "name": "LOOT - Load Order Optimization",
        "files": [
            ("sky_claw/loot/__init__.py", "Inicialización del módulo loot"),
            ("sky_claw/loot/cli.py", "CLI de LOOT"),
            ("sky_claw/loot/masterlist.py", "Masterlist de LOOT - reglas de ordenamiento"),
            ("sky_claw/loot/parser.py", "Parser de masterlist de LOOT"),
        ],
    },
    "14_discovery_loot_fomod_mo2/fomod.txt": {
        "name": "FOMOD - Instalador de Mods",
        "files": [
            ("sky_claw/fomod/__init__.py", "Inicialización del módulo fomod"),
            ("sky_claw/fomod/installer.py", "Instalador FOMOD"),
            ("sky_claw/fomod/models.py", "Modelos de datos FOMOD"),
            ("sky_claw/fomod/parser.py", "Parser de archivos FOMOD XML"),
            ("sky_claw/fomod/resolver.py", "Resolvedor de dependencias FOMOD"),
        ],
    },
    "14_discovery_loot_fomod_mo2/mo2.txt": {
        "name": "MO2 - Mod Organizer 2",
        "files": [
            ("sky_claw/mo2/__init__.py", "Inicialización del módulo mo2"),
            ("sky_claw/mo2/vfs.py", "Virtual File System de Mod Organizer 2"),
        ],
    },
    "14_discovery_loot_fomod_mo2/assets.txt": {
        "name": "Assets - Escáner de Recursos",
        "files": [
            ("sky_claw/assets/__init__.py", "Inicialización del módulo assets"),
            ("sky_claw/assets/asset_scanner.py", "Scanner de assets del juego"),
        ],
    },
    # ── 15 VALIDATORS ────────────────────────────────────────────────────────
    "15_validators/validators_all.txt": {
        "name": "Validadores",
        "files": [
            ("sky_claw/validators/__init__.py", "Inicialización del módulo validators"),
            ("sky_claw/validators/safe_save_validator.py", "Validador de guardado seguro"),
        ],
    },
    # ── 16 TESTS ─────────────────────────────────────────────────────────────
    "16_tests/tests_core.txt": {
        "name": "Tests - Core y Base de Datos",
        "files": [
            ("tests/__init__.py", "Inicialización del paquete tests"),
            ("tests/conftest.py", "Configuración global de pytest"),
            ("tests/test_core_event_bus.py", "Test: event bus"),
            ("tests/test_contracts_ticket_1_1.py", "Test: contratos"),
            ("tests/test_schemas.py", "Test: schemas"),
            ("tests/test_dlq_manager.py", "Test: DLQ manager"),
            ("tests/test_event_bus_dlq_integration.py", "Test: integración event bus + DLQ"),
            ("tests/test_async_path_resolver.py", "Test: path resolver asíncrono"),
            ("tests/test_path_resolution_service.py", "Test: servicio de resolución de rutas"),
            ("tests/test_circuit_breaker.py", "Test: circuit breaker"),
            ("tests/test_lazy_init.py", "Test: inicialización lazy"),
            ("tests/test_db_integrity.py", "Test: integridad de base de datos"),
            ("tests/test_async_registry.py", "Test: registro asíncrono"),
            ("tests/test_distributed_locks.py", "Test: locks distribuidos"),
            ("tests/test_journal.py", "Test: journaling"),
            ("tests/test_registry.py", "Test: registro de mods"),
            ("tests/test_safe_save_validator.py", "Test: validador de guardado seguro"),
            ("tests/test_exe_config_sandbox.py", "Test: sandbox de configuración exe"),
        ],
    },
    "16_tests/tests_agent.txt": {
        "name": "Tests - Agente AI",
        "files": [
            ("tests/test_agent_guardrail.py", "Test: guardrails del agente"),
            ("tests/test_agent_tools.py", "Test: herramientas del agente"),
            ("tests/test_autogen_integration.py", "Test: integración AutoGen"),
            ("tests/test_hermes_parser.py", "Test: parser Hermes"),
            ("tests/test_hermes_router.py", "Test: router Hermes"),
            ("tests/test_hermes_tool_registry.py", "Test: registro de herramientas Hermes"),
            ("tests/test_lcel_stability.py", "Test: estabilidad LCEL"),
            ("tests/test_providers.py", "Test: proveedores LLM"),
            ("tests/test_router.py", "Test: router del agente"),
        ],
    },
    "16_tests/tests_integration.txt": {
        "name": "Tests - Integración y Orquestador",
        "files": [
            ("tests/test_main.py", "Test: entry point principal"),
            ("tests/test_auto_detect.py", "Test: auto-detección"),
            ("tests/test_state_graph.py", "Test: grafo de estados"),
            ("tests/test_state_graph_loop_integration.py", "Test: integración grafo + loops"),
            ("tests/test_supervisor_dispatch_tool.py", "Test: dispatch del supervisor"),
            ("tests/test_sync_engine.py", "Test: motor de sincronización"),
            ("tests/test_sync_engine_resilience.py", "Test: resiliencia del sync engine"),
            ("tests/test_tool_dispatcher.py", "Test: dispatcher de herramientas"),
            ("tests/test_tool_strategies_middleware.py", "Test: middleware de estrategias"),
            ("tests/test_patch_integration.py", "Test: integración de parches"),
            ("tests/test_operations_hub_ws.py", "Test: WebSocket del Operations Hub"),
            ("tests/test_web_app.py", "Test: aplicación web"),
            ("tests/test_web.py", "Test: módulo web"),
        ],
    },
    "16_tests/tests_tools.txt": {
        "name": "Tests - Tools y Runners",
        "files": [
            ("tests/test_conflict_analyzer.py", "Test: analizador de conflictos"),
            ("tests/test_dyndolod_service.py", "Test: servicio DynDOLOD"),
            ("tests/test_synthesis_service.py", "Test: servicio Synthesis"),
            ("tests/test_vramr_service.py", "Test: servicio VRAMR"),
            ("tests/test_xedit_service.py", "Test: servicio xEdit"),
            ("tests/test_xedit.py", "Test: módulo xEdit"),
            ("tests/test_tools.py", "Test: herramientas generales"),
            ("tests/test_tools_installer.py", "Test: instalador de herramientas"),
            ("tests/test_pyinstaller.py", "Test: build PyInstaller"),
            ("tests/test_fomod_installer.py", "Test: instalador FOMOD"),
            ("tests/test_fomod.py", "Test: módulo FOMOD"),
            ("tests/test_loot.py", "Test: módulo LOOT"),
            ("tests/test_masterlist.py", "Test: masterlist"),
            ("tests/test_vfs.py", "Test: Virtual File System"),
            ("tests/test_nexus_downloader.py", "Test: descargador Nexus"),
            ("tests/test_reddit_client.py", "Test: cliente Reddit"),
            ("tests/test_telegram.py", "Test: módulo Telegram"),
            ("tests/test_watcher_daemon_eventbus.py", "Test: watcher daemon + event bus"),
        ],
    },
    "16_tests/tests_security.txt": {
        "name": "Tests - Seguridad",
        "files": [
            ("tests/test_hitl.py", "Test: Human-in-the-Loop"),
            ("tests/test_hitl_wiring.py", "Test: wiring de HITL"),
            ("tests/test_auth_token_manager.py", "Test: gestor de tokens"),
            ("tests/test_credential_vault.py", "Test: bóveda de credenciales"),
            ("tests/test_loop_guardrail.py", "Test: protección de loops"),
            ("tests/test_network_gateway.py", "Test: gateway de red"),
            ("tests/test_path_validator.py", "Test: validador de rutas"),
            ("tests/test_sanitize.py", "Test: sanitización"),
        ],
    },
    "16_tests/tests_frontend.txt": {
        "name": "Tests - Frontend",
        "files": [
            ("tests/frontend/appstate_smoke.mjs", "Test smoke: estado de la app"),
            ("tests/frontend/log_view_smoke.mjs", "Test smoke: visor de logs"),
            ("tests/frontend/phase5_binders_smoke.mjs", "Test smoke: binders fase 5"),
        ],
    },
    "16_tests/tests_fixtures.txt": {
        "name": "Tests - Fixtures",
        "files": [
            ("tests/fixtures/fomod/simple.xml", "Fixture FOMOD: XML simple"),
            ("tests/fixtures/fomod/complex.xml", "Fixture FOMOD: XML complejo"),
            ("tests/fixtures/fomod/conditional.xml", "Fixture FOMOD: XML condicional"),
        ],
    },
    # ── 17 SCRIPTS ───────────────────────────────────────────────────────────
    "17_scripts/scripts_all.txt": {
        "name": "Scripts Auxiliares",
        "files": [
            ("scripts/chaos_test.js", "Test de caos - inyección de fallos"),
            ("scripts/first_run.py", "Script de primera ejecución"),
            ("scripts/fix_agent_panel_deep.ps1", "Fix: panel del agente (profundo)"),
            ("scripts/fix_gemini_panel.ps1", "Fix: panel de Gemini"),
            ("scripts/restart_agent.ps1", "Script: reinicio del agente"),
            ("scripts/run_chaos_suite.ps1", "Script: ejecución de suite de caos"),
            ("scripts/scrape_project.py", "Script: scraping del proyecto"),
            ("scripts/setup_env.ps1", "Script: configuración de entorno"),
            ("scripts/sre_agent_panel_repair.ps1", "Script: reparación panel SRE"),
            ("scripts/watcher_daemon.ps1", "Script: daemon vigilante (PowerShell)"),
        ],
    },
}


def main():
    print("=" * 80)
    print("SKY CLAW SYNC - Extracción Modular de Código Fuente")
    print(f"Fecha: {EXTRACTION_DATE}")
    print(f"Directorio base: {BASE_DIR}")
    print(f"Directorio de salida: {OUTPUT_DIR}")
    print("=" * 80)

    # Statistics
    total_files = 0
    total_size = 0
    extracted_files = 0
    skipped_files = 0
    failed_files = []
    module_stats = {}

    for output_rel, module_def in MODULES.items():
        module_name = module_def["name"]
        source_files = module_def["files"]
        output_path = OUTPUT_DIR / output_rel

        print(f"\n{'─' * 60}")
        print(f"Módulo: {module_name}")
        print(f"Salida: {output_rel}")
        print(f"Archivos fuente: {len(source_files)}")

        entries = []
        module_file_count = 0

        for src_rel, description in source_files:
            src_path = BASE_DIR / src_rel
            total_files += 1

            if not src_path.exists():
                print(f"  [SKIP] No existe: {src_rel}")
                skipped_files += 1
                failed_files.append((src_rel, "archivo no encontrado"))
                continue

            content = read_file_safe(src_path)
            if content is None:
                skipped_files += 1
                failed_files.append((src_rel, "no se pudo leer/decodificar"))
                continue

            entry = format_file_entry(src_rel, content, description)
            entries.append(entry)
            module_file_count += 1
            extracted_files += 1
            total_size += len(content.encode("utf-8"))

            ext = src_path.suffix
            size_kb = src_path.stat().st_size / 1024
            print(f"  [OK] {src_rel} ({ext}, {size_kb:.1f} KB)")

        if entries:
            write_module_file(output_path, module_name, entries)
            print(f"  → Escrito: {output_path} ({module_file_count} archivos)")
        else:
            print("  [WARN] Sin archivos válidos para este módulo")

        module_stats[output_rel] = {
            "name": module_name,
            "total": len(source_files),
            "extracted": module_file_count,
        }

    # ── GENERATE INDEX ───────────────────────────────────────────────────────
    print(f"\n{'=' * 80}")
    print("Generando archivo índice...")

    index_path = OUTPUT_DIR / "00_INDICE.txt"
    separator = "=" * 80

    index_content = f"""{separator}
SKY CLAW SYNC - Índice de Extracción de Código Fuente
Fecha de extracción: {EXTRACTION_DATE}
Directorio base del proyecto: {BASE_DIR}
Directorio de salida: {OUTPUT_DIR}
{separator}

RESUMEN GENERAL
{"─" * 40}
Total de archivos fuente procesados: {total_files}
Archivos extraídos exitosamente: {extracted_files}
Archivos omitidos/fallidos: {skipped_files}
Tamaño total extraído: {total_size / 1024:.1f} KB ({total_size / (1024 * 1024):.2f} MB)
Total de módulos: {len(MODULES)}
Total de archivos .txt generados: {sum(1 for s in module_stats.values() if s["extracted"] > 0)}

MAPA DE MÓDULOS
{"─" * 40}

"""

    for i, (output_rel, stats) in enumerate(module_stats.items(), 1):
        status = "✓" if stats["extracted"] > 0 else "✗"
        index_content += (
            f"{i:02d}. [{status}] {output_rel}\n"
            f"    Nombre: {stats['name']}\n"
            f"    Archivos: {stats['extracted']}/{stats['total']} extraídos\n\n"
        )

    if failed_files:
        index_content += f"\nARCHIVOS OMITIDOS/FALLIDOS\n{'─' * 40}\n"
        for src, reason in failed_files:
            index_content += f"  • {src} — Razón: {reason}\n"

    index_content += f"""

ESTRUCTURA DE ARQUITECTURA
{"─" * 40}

El proyecto Sky Claw Sync sigue una arquitectura modular con las siguientes capas:

1. FRONTEND (01_frontend/)
   - Interfaz web HTML/CSS/JS
   - Múltiples temas (Dark Glass, Medieval)
   - Comunicación vía WebSocket

2. BACKEND CORE (02_backend_core/)
   - Modelos, schemas, event bus
   - VFS (Virtual File System)
   - Resolución de rutas, DLQ
   - Contratos y manejo de errores

3. BASE DE DATOS (03_database/)
   - Registro de mods (síncrono y asíncrono)
   - Journaling para atomicidad
   - Locks distribuidos
   - Rollback y snapshots

4. AGENTE AI (04_agent_ai/)
   - Ejecutor con routing semántico
   - Integración AutoGen y LangChain
   - Sistema de razonamiento (Tree of Thought)
   - Tools especializadas (DB, Nexus, Sistema)

5. ORQUESTADOR (05_orchestrator/)
   - Supervisor de alto nivel
   - Motor de sincronización
   - Grafo de estados (LangGraph)
   - Daemons (maintenance, telemetry, watcher)
   - Estrategias de herramientas (9 estrategias)

6. APIs Y CONTROLADORES (06_api_controllers/)
   - Servidor web (FastAPI/Starlette)
   - Controladores GUI (Chat, Mod, Navigation)
   - Gateway Node.js + Telegram Gateway

7. COMUNICACIONES (07_communications/)
   - Telegram (bot, polling, sender)
   - WebSocket daemon
   - Bridge frontend

8. SEGURIDAD (08_security/)
   - Gobernanza y HITL
   - Guardrails (agente, loops, metacognitivo)
   - Autenticación y bóveda de credenciales
   - Network gateway y validación de rutas
   - Scanners (Purple Team, sanitización)

9. TOOLS/RUNNERS (09_tools_runners/)
   - BodySlide, DynDOLOD, Pandora
   - Synthesis, Wrye Bash, xEdit
   - Pipeline de patchers
   - Scripts Pascal para xEdit

10. SCRAPER (10_scraper/)
    - Nexus Mods scraper/downloader
    - Reddit client
    - Agente de scraping autónomo

11. GUI DESKTOP (11_gui/)
    - Aplicación tkinter/customtkinter
    - Sistema de vistas MVC
    - Componentes reutilizables
    - Comunicación con agente

12. CONFIGURACIONES (12_config/)
    - pyproject.toml, requirements
    - Configuración de app y logging
    - Build scripts

13. MODOS DE EJECUCIÓN (13_modes/)
    - CLI, GUI, Web, Telegram, Security

14. DISCOVERY/LOOT/FOMOD/MO2 (14_discovery_loot_fomod_mo2/)
    - Detección de entorno
    - LOOT (Load Order Optimization)
    - FOMOD (instalador de mods)
    - MO2 (Mod Organizer 2 VFS)

15. VALIDADORES (15_validators/)
    - Validación de guardado seguro

16. TESTS (16_tests/)
    - Tests unitarios e integración
    - Fixtures FOMOD XML

17. SCRIPTS (17_scripts/)
    - Scripts PowerShell, Python, JavaScript
    - Fixes, setup, chaos testing

{separator}
Fin del índice
{separator}
"""

    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text(index_content, encoding="utf-8")
    print(f"Índice generado: {index_path}")

    # ── FINAL SUMMARY ────────────────────────────────────────────────────────
    print(f"\n{'=' * 80}")
    print("EXTRACCIÓN COMPLETADA")
    print(f"{'=' * 80}")
    print(f"Archivos procesados: {total_files}")
    print(f"Archivos extraídos: {extracted_files}")
    print(f"Archivos omitidos: {skipped_files}")
    print(f"Tamaño total: {total_size / 1024:.1f} KB ({total_size / (1024 * 1024):.2f} MB)")
    print(f"Módulos generados: {len(module_stats)}")
    print(f"Directorio de salida: {OUTPUT_DIR}")

    if failed_files:
        print(f"\nAdvertencias ({len(failed_files)}):")
        for src, reason in failed_files:
            print(f"  • {src}: {reason}")

    return 0 if not failed_files else 1


if __name__ == "__main__":
    sys.exit(main())

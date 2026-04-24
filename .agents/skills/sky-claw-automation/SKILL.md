---
name: sky-claw-automation
description: "Automatiza el ciclo de vida de mods de Skyrim: scraping en Nexus Mods, gestión de base de datos SQLite y notificaciones vía Telegram. Úsalo para buscar actualizaciones, validar compatibilidades y ejecutar tareas de mantenimiento en App-nexus."
metadata:
  version: 2.0.0
  last_updated: 2026-04-23
  compatibility:
    - Python 3.11+
    - SQLite
    - Playwright/Selenium
    - Telegram Bot API
---

# Sky-Claw: Skyrim Modding Automation Skill

Esta habilidad permite al agente operar como un backend inteligente para la gestión de mods de Skyrim (PC), integrando automatización de navegador (Playwright/Selenium) con la lógica de negocio de App-nexus.

## Ámbito de Aplicación (When to use)

- **Sincronización de Metadatos:** Actualización de versiones, requisitos y logs de cambios desde Nexus Mods.
- **Gestión de Dependencias:** Identificación de Master Files (.esm) y parches necesarios.
- **Mantenimiento de Base de Datos:** Operaciones CRUD en la base SQLite local de App-nexus.
- **Monitoreo de Estado:** Reporte de errores de scraping o disponibilidad de archivos mediante el bot de Telegram.

## Flujo de Decisión (Decision Tree)

1. **¿La tarea requiere datos externos?**
   - SÍ: Ejecutar scripts de scraping en Nexus Mods.
   - NO: Pasar al punto 2.
2. **¿Los datos están en la DB local?**
   - SÍ: Realizar consulta SQL eficiente.
   - NO: Intentar recuperación vía API/Web Scraping.
3. **¿Se requiere interacción con el usuario?**
   - SÍ: Formatear salida para Telegram Bot.
   - NO: Ejecutar tarea en segundo plano y loguear en SQLite.

## Protocolo de Ejecución

### 1. Scraping y Automatización de Navegador
- **Identificación:** Utilizar siempre el `mod_id` de Nexus como clave primaria.
- **Eficiencia:** Implementar esperas explícitas (explicit waits) para evitar bloqueos por carga de DOM.
- **Seguridad:** No exponer credenciales de Nexus en logs. Utilizar variables de entorno.

### 2. Gestión de Datos (SQLite)
- **Integridad:** Validar esquemas antes de cada `INSERT` o `UPDATE`.
- **Performance:** Usar transacciones para actualizaciones masivas de la lista de mods.
- **Estructura:** Mantener consistencia con el esquema de `App-nexus`.

### 3. Interfaz de Telegram
- **Formato:** Los mensajes deben ser concisos, utilizando Markdown para resaltar versiones y nombres de mods.
- **Alertas:** Notificar inmediatamente fallos de autenticación o cambios en los términos de servicio de Nexus que afecten el scraping.

## Convenciones Técnicas

- **Control de Versiones:** Los scripts en `scripts/` deben ser modulares.
- **Manejo de Errores:** Aplicar Root Cause Analysis (RCA). Si un scraping falla, identificar si es por cambio de UI en Nexus o por timeout de red.
- **Nomenclatura:** Seguir estándar de Python (PEP 8) para scripts de automatización.

## Recursos Disponibles

| Recurso | Tipo | Descripción |
|---------|------|-------------|
| `scripts/scrape_project.py` | Script Python | Scraping genérico de metadatos de proyecto |
| `scripts/setup_env.ps1` | PowerShell | Configuración de entorno y dependencias |
| `scripts/first_run.py` | Script Python | Inicialización de base de datos y configuración |
| `sky_claw/db/` | Módulo Python | Lógica de persistencia SQLite |
| `sky_claw/comms/` | Módulo Python | Integración con Telegram y notificaciones |
| `logs/sky_claw.log` | Log | Registro de operaciones del sistema |

> **Nota:** Los scripts `nexus_scraper.py`, `db_manager.py` y `telegram_reports.json` mencionados en versiones anteriores han sido consolidados en los módulos del paquete `sky_claw/`. No referenciar archivos inexistentes.

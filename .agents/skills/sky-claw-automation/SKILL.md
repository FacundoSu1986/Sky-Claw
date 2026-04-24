---
name: sky-claw-automation
description: "Automatiza el ciclo de vida de mods de Skyrim: scraping en Nexus Mods, gestión de base de datos SQLite y notificaciones vía Telegram. Úsalo para buscar actualizaciones, validar compatibilidades y ejecutar tareas de mantenimiento en App-nexus."
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
- `/scripts/nexus_scraper.py`: Script base para extracción de datos.
- `/scripts/db_manager.py`: Utilidad para interactuar con SQLite.
- `/templates/telegram_reports.json`: Plantillas de mensajes para el bot.

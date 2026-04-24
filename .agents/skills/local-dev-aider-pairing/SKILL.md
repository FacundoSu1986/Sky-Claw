---
name: local-dev-aider-pairing
description: Pair programming con Aider para modificación de código integrada con Git, generación de parches, y refactorización local-first. Usar para parchear mods, crear scripts Papyrus, o refactorizar lógica interna de Sky-Claw. No usar para cambios triviales que no justifiquen la sobrecarga de Aider.
---

# Local Dev Aider Pairing

Habilita pair programming con Aider para modificación de código local-first integrada con Git, parches de mods, y refactorización de Sky-Claw.

## Cuándo Usar

- Cuando Sky-Claw necesite parchear automáticamente un problema de compatibilidad en un plugin de mod.
- Cuando el usuario solicite un script personalizado para un mod (ej: scripting Papyrus).
- Para refactorizar la lógica interna de Sky-Claw manteniendo historial limpio de Git.
- Al generar parches de compatibilidad basados en reportes de conflictos.

## Cuándo NO Usar

- Para cambios de una sola línea que no justifiquen la sobrecarga.
- Cuando el cambio no requiera integración con Git.
- Para revisión de código sin intención de modificar.

## Instrucciones

### 1. Configuración
```bash
pip install aider-chat
```

### 2. Contextualización
- Lanzar Aider en la carpeta de desarrollo del mod o de Sky-Claw.
- Usar `/add` para incluir archivos relevantes (ej: `main.py`, archivos `.psc`).

### 3. Flujo de Trabajo
```bash
# 1. SIEMPRE hacer commit antes de cambios sustanciales
git commit -am "Pre-aider checkpoint"

# 2. Lanzar Aider con archivos objetivo
aider sky_claw/agent/router.py sky_claw/core/db.py

# 3. Proponer cambios basados en reportes de conflictos de mods
/add conflict_report.json
```

### 4. Reglas de Oro
- **Soberanía Git:** Siempre commitear antes de permitir que Aider haga cambios sustanciales.
- **Deltas sobre reescrituras:** Focalizarse en modificaciones pequeñas y quirúrgicamente precisas.
- **Loop de revisión:** Usar Antigravity para revisar el output de Aider antes de aplicar al branch principal.
- **Conocimiento del VFS:** Informar a Aider sobre las limitaciones del sistema de archivos virtual (VFS) de MO2.

## Recursos

- Integración con el ecosistema Sky-Claw vía `specialized_bridges.py`.

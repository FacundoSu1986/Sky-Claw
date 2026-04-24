---
name: local-automation-interpreter
description: Automatiza herramientas de modding de Windows (LOOT, xEdit, MO2) usando Open Interpreter con ejecución sandboxed. Usar cuando Sky-Claw necesite ejecutar scripts PowerShell/CMD para parchear mods, manipular carpetas, o actualizar el registro de mods. No usar para tareas que no requieran interacción con el sistema de archivos o binarios de modding.
---

# Local Automation Interpreter

Orquesta la ejecución segura de herramientas de modding de Windows (LOOT, xEdit, MO2) mediante la interfaz de lenguaje natural de Open Interpreter.

## Cuándo Usar

- Cuando Sky-Claw necesite ejecutar scripts complejos de PowerShell o CMD para parchear archivos de mods.
- Cuando el usuario dé instrucciones de alto nivel como "Limpia todos los archivos .esm con xEdit y luego ordena con LOOT."
- Para tareas multi-paso que involucren manipulación de carpetas y actualizaciones al registro de mods.
- Al orquestar secuencias de herramientas de modding que requieran coordinación.

## Cuándo NO Usar

- Para operaciones simples de lectura que no modifiquen el sistema de archivos.
- Cuando la tarea pueda resolverse con una consulta SQL a la base de datos local.
- Para tareas de IA/LLM que no requieran interacción con binarios del sistema.

## Instrucciones

### 1. Configuración del Motor
```bash
pip install open-interpreter
```

### 2. Reglas de Seguridad (Mandatorio)
- **Auto-run deshabilitado:** `interpreter.auto_run = False` siempre. El `ASTGuardian` o el usuario deben verificar antes de ejecutar.
- **Sin permisos de administrador:** Ejecutar siempre en contexto no-admin cuando sea posible.
- **Backup obligatorio:** Antes de ejecutar cualquier automatización, verificar que los perfiles de MO2 estén respaldados.
- **HITL para destructivas:** Operaciones de borrado, movimiento masivo de archivos, o cambios de configuración requieren confirmación explícita del usuario.

### 3. Delegación de Tareas
```python
# Traducir intención del usuario a comandos del sistema
result = interpreter.chat(user_message)

# Apuntar a binarios específicos usando rutas absolutas de config.py
interpreter.chat(f"Ejecuta xEdit en {config.XEDIT_PATH} con el plugin {plugin_name}")
```

### 4. Interacción con Herramientas
- Usar rutas absolutas del `config.py` de Sky-Claw para localizar binarios.
- Implementar esperas explícitas para procesos de larga duración.
- Capturar stdout/stderr para telemetría y diagnóstico.

## Recursos

- `scripts/` — Scripts de automatización reutilizables.

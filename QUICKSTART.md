# Sky-Claw: Guía Rápida de Inicio

> **Audiencia:** primer arranque desde fuentes en Windows.
> **Estado:** implementado con validación real pendiente para el rig MO2/USVFS
> concreto de cada usuario.
> **Fuentes canónicas:** `build.bat`, `sky_claw/__main__.py`,
> `sky_claw/config.py` y `local_scripts/scripts/first_run.py`.
> **Última verificación:** 2026-07-25 sobre `origin/main` `c6ab35e`.

Esta guía prepara el plano de control Sky-Claw para una primera ejecución segura.

## 1. Requisitos
- **Python >= 3.11** (lo que exige `pyproject.toml`; CI valida con 3.11 y 3.12).
- **MO2 (Mod Organizer 2)** instalado y configurado para Skyrim Special Edition.
- **Conexión a Internet** para descargar mods y contactar con la IA.

## 2. Instalación
Ejecutá el script de construcción para crear el entorno virtual (`.venv\`) e instalar las dependencias necesarias:
```batch
build.bat
```

**Activá el entorno virtual** antes de los comandos siguientes — `build.bat` lo
activa solo para sí mismo (`setlocal`), así que en tu terminal hay que activarlo
a mano. Si no, `python` usa el intérprete del sistema (sin `sky_claw` instalado)
y los comandos fallan con `ModuleNotFoundError`:
```batch
.\.venv\Scripts\activate
```
*(Alternativa sin activar: prefijá cada comando con
`.\.venv\Scripts\python.exe` en vez de `python`.)*

## 3. Configuración Inicial
Sky-Claw ahora usa un asistente interactivo para que no tengas que editar archivos a mano. La configuración se guarda automáticamente en `~/.sky_claw/config.toml`.

Corré el siguiente comando y seguí las instrucciones:
```bash
python local_scripts/scripts/first_run.py
```
*Aquí podrás elegir tu proveedor de IA (Claude/Anthropic, DeepSeek, OpenAI u Ollama) e ingresar tus API Keys.*

## 4. Modos de Ejecución

### Modo Gráfico (GUI) 🎨
La opción recomendada para usuarios que prefieren una interfaz visual moderna.
```bash
python -m sky_claw --mode gui
```

### Modo Telegram 📱
Para manejar tus mods desde el celular con botones interactivos de aprobación (HITL).
```bash
python -m sky_claw --mode telegram
```

### Modo Terminal (CLI) 💻
Ideal para desarrolladores y uso rápido.
```bash
python -m sky_claw --mode cli
```

## 5. Seguridad y HITL

Sky-Claw aplica controles **Human-in-the-Loop** en las rutas que cablean un
gate de aprobación. No asumas que toda acción o descarga tiene HITL por el solo
hecho de usar GUI o Telegram: verificá el preflight y la solicitud concreta.

---
Antes de operar un perfil real, completa el
[workflow seguro](docs/user/safe_workflows.md) y el smoke de
[validación en rig real](docs/operations/real_rig_validation.md). Haber iniciado
la aplicación no prueba por sí solo el bridge MO2/USVFS ni las tools externas.

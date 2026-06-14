# Sky-Claw: Guía Rápida de Inicio

¡Bienvenido a la versión moderna de Sky-Claw! Esta guía te ayudará a configurar el agente en pocos minutos.

## 1. Requisitos
- **Python >= 3.11** (lo que exige `pyproject.toml`; CI valida con 3.11 y 3.12).
- **MO2 (Mod Organizer 2)** instalado y configurado para Skyrim Special Edition.
- **Conexión a Internet** para descargar mods y contactar con la IA.

## 2. Instalación
Ejecutá el script de construcción para crear el entorno virtual (`venv\`) e instalar las dependencias necesarias:
```batch
build.bat
```

**Activá el entorno virtual** antes de los comandos siguientes — `build.bat` lo
activa solo para sí mismo (`setlocal`), así que en tu terminal hay que activarlo
a mano. Si no, `python` usa el intérprete del sistema (sin `sky_claw` instalado)
y los comandos fallan con `ModuleNotFoundError`:
```batch
venv\Scripts\activate
```
*(Alternativa sin activar: prefijá cada comando con `venv\Scripts\python` en vez de `python`.)*

## 3. Configuración Inicial
Sky-Claw ahora usa un asistente interactivo para que no tengas que editar archivos a mano. La configuración se guarda automáticamente en `~/.sky_claw/config.toml`.

Corré el siguiente comando y seguí las instrucciones:
```bash
python local_scripts/scripts/first_run.py
```
*Aquí podrás elegir tu proveedor de IA (Claude/Anthropic, DeepSeek u Ollama) e ingresar tus API Keys.*

> ⚠️ El asistente puede listar además `openai`, pero el runtime **no** lo soporta
> todavía — elegí uno de los tres de arriba. (Quitar/cablear esa opción es un fix
> de código pendiente.)

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
Sky-Claw es un agente **Human-in-the-Loop**. Para cualquier descarga desde hosts externos (GitHub, Patreon, Mega), el bot te pedirá aprobación vía Telegram antes de proceder.

---
¡Eso es todo! Ahora Sky-Claw está listo para organizar tu Load Order.

# Sky-Claw: Guía Rápida de Inicio

¡Bienvenido a la versión moderna de Sky-Claw! Esta guía te ayudará a configurar el agente en pocos minutos.

## 1. Requisitos
- **Python 3.11 o 3.12** (lo que valida CI; `pyproject.toml` exige `>=3.11`).
- **MO2 (Mod Organizer 2)** instalado y configurado para Skyrim Special Edition.
- **Conexión a Internet** para descargar mods y contactar con la IA.

## 2. Instalación
Ejecutá el script de construcción para crear el entorno virtual e instalar las dependencias necesarias:
```batch
build.bat
```

## 3. Configuración Inicial
Sky-Claw ahora usa un asistente interactivo para que no tengas que editar archivos a mano. La configuración se guarda automáticamente en `~/.sky_claw/config.toml`.

Corré el siguiente comando y seguí las instrucciones:
```bash
python local_scripts/scripts/first_run.py
```
*Aquí podrás elegir tu proveedor de IA (Claude/Anthropic, DeepSeek u Ollama) e ingresar tus API Keys.*

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

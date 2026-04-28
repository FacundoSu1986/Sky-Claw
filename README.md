# Sky-Claw

![Python 3.14+](https://img.shields.io/badge/Python-3.14%2B-blue)
![License MIT](https://img.shields.io/badge/License-MIT-green)
![Tests](https://img.shields.io/badge/Tests-Passing-brightgreen)

---

## 🚀 Descripción

Sky-Claw es un agente autónomo avanzado que gestiona mods de Skyrim SE/AE a través de Mod Organizer 2 (MO2). Permite buscar, descargar, instalar y resolver conflictos de mods usando lenguaje natural.

**Novedades de la Versión Moderna:**
- **Soporte Multi-LLM**: Elegí entre Anthropic (Claude), OpenAI (GPT-4), DeepSeek o ejecución local con Ollama.
- **Interfaz Gráfica (GUI)**: Nueva ventana moderna basada en Tkinter y `sv-ttk` para una gestión visual.
- **Configuración TOML**: Gestión simplificada en `~/.sky_claw/config.toml`.
- **Asistente de Inicio**: Configuración guiada automática con `scripts/first_run.py`.
- **Seguridad HITL**: Aprobación interactiva vía botones de Telegram para descargas externas.

---

## 🏗️ Arquitectura Moderna

```
Usuario (GUI / CLI / Telegram)
         |
    LLMRouter (Mensajería + Tool Dispatch)
         |
    LLMProvider (Interfaz Unificada)
    |-- AnthropicProvider
    |-- OpenAIProvider
    |-- DeepSeekProvider
    |-- OllamaProvider
         |
   AsyncToolRegistry
   |-- search_mod        -> AsyncModRegistry (SQLite)
   |-- check_load_order  -> MO2Controller (modlist.txt)
   |-- detect_conflicts  -> SQL JOIN sobre dependencias
   |-- run_loot_sort     -> LOOTRunner
   |-- run_xedit_script  -> XEditRunner
   |-- download_mod      -> NexusDownloader + HITLGuard
         |
    MO2 Portable / Skyrim SE
```

---

## 📦 Instalación

Sky-Claw incluye scripts automáticos para facilitar la instalación:

1. **Clonar y Construir**:
   ```batch
   git clone https://github.com/FacundoSu1986/sky-claw.git
   cd sky-claw
   build.bat
   ```

2. **Configurar**:
   Ejecutá el asistente para configurar tus API Keys y detectar tus rutas de MO2:
   ```bash
   python scripts/first_run.py
   ```

---

## 🎮 Uso

### Modo Gráfico (GUI)
```bash
python -m sky_claw --mode gui
```

### Modo Telegram (HITL Interactivo)
```bash
python -m sky_claw --mode telegram
```

### Modo Terminal (CLI)
```bash
python -m sky_claw --mode cli
```

---

## 🛡️ Seguridad Zero-Trust

Sky-Claw aplica una política de seguridad estricta:
- **NetworkGateway**: Solo permite conexiones a dominios autorizados (`*.nexusmods.com`, `api.telegram.org`, `openai.com`, etc.).
- **HITLGuard**: El agente pausa su ejecución y solicita aprobación mediante botones de Telegram antes de descargar archivos desde hosts externos como GitHub, Patreon o Mega.
- **Sandboxing**: Todas las operaciones de archivo están restringidas al directorio de MO2 y carpetas de instalación autorizadas.

---

## ✅ Roadmap (Estado Actual)

- [x] Soporte Multi-LLM (OpenAI, Anthropic, DeepSeek, Ollama)
- [x] Interfaz Gráfica Moderna (sv-ttk)
- [x] Configuración centralizada TOML
- [x] Asistente interactivo de primera ejecución
- [x] HITL con botones interactivos en Telegram
- [x] Base de datos async distribuida
- [x] Wrapper xEdit y LOOT headless
- [x] Parser y resolución FOMOD
- [ ] Empaquetado final como .exe único

---

## 📄 Licencia

MIT

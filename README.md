# Sky-Claw

> **Audiencia:** usuarios, operadores y contribuidores.
>
> **Estado:** producto en desarrollo; las garantías por subsistema se detallan
> en la documentación enlazada.
>
> **Fuentes canónicas:** runtime, ADR vigentes, `DEPLOYMENT.md` y `SECURITY.md`.
>
> **Última verificación:** 2026-07-25 sobre `origin/main` `c6ab35e`.

![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-blue)
![License MIT](https://img.shields.io/badge/License-MIT-green)
![Tests](https://img.shields.io/badge/Tests-Passing-brightgreen)

---

## 🚀 Descripción

Sky-Claw es un plano de control local asistido por IA para gestionar mods de
Skyrim SE/AE a través de Mod Organizer 2 (MO2). Permite buscar, descargar,
instalar y analizar conflictos usando lenguaje natural, con locks, trazabilidad
y gates humanos en las operaciones que los requieren. Su norte es una
**caja negra de vuelo**, no un agente autónomo irrestricto.

**Novedades de la Versión Moderna:**
- **Soporte Multi-LLM**: Elegí entre Anthropic (Claude), OpenAI (GPT-4), DeepSeek o ejecución local con Ollama.
- **Interfaz Gráfica (GUI)**: Interfaz moderna basada en NiceGUI (web/escritorio) para una gestión visual.
- **Configuración TOML**: Gestión simplificada en `~/.sky_claw/config.toml`.
- **Asistente de Inicio**: Configuración guiada automática con `local_scripts/scripts/first_run.py`.
- **Seguridad HITL**: Aprobación interactiva en los handlers y rituales que
  cablean un gate humano.

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
   |-- run_loot_sort     -> VfsExecutionBroker -> worker LOOT bajo MO2/USVFS
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
   python local_scripts/scripts/first_run.py
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
- **HITLGuard**: `download_mod` y los rituales que lo inyectan esperan una
  decisión humana; una ruta sin ese wiring no hereda HITL automáticamente.
- **Sandboxing**: Todas las operaciones de archivo están restringidas al directorio de MO2 y carpetas de instalación autorizadas.

---

## 👨‍💻 Para Desarrolladores

Sky-Claw es un ecosistema asíncrono complejo. Si deseas contribuir, añadir nuevas herramientas o entender cómo funciona el orquestador por dentro, consulta la siguiente documentación:

- **[Arquitectura del Sistema](ARCHITECTURE.md):** Topología de capas, flujo de datos asíncrono y modelo de seguridad.
- **[Guía de Contribución](CONTRIBUTING.md):** Setup del entorno de desarrollo, flujo de trabajo TDD, convenciones de código y proceso de PRs.
- **[Manual de Agentes y Tools](docs/agents/tool_creation.md):** Tutorial para extender el sistema con nuevos Tool Runners y proveedores LLM.
- **[SOP del Pipeline de Modding](sky_claw/local/AGENTS.md):** Reglas de negocio inmutables para la orquestación de herramientas de Skyrim.
- **[Portal de Documentación](docs/README.md):** Guías para usuarios, operadores, desarrolladores y agentes.

---

## ✅ Roadmap (Estado Actual)

- [x] Soporte Multi-LLM (OpenAI, Anthropic, DeepSeek, Ollama)
- [x] Interfaz Gráfica Moderna (NiceGUI)
- [x] Configuración centralizada TOML
- [x] Asistente interactivo de primera ejecución
- [x] HITL en handlers y rituales cableados a un gate humano
- [x] Base de datos async distribuida
- [x] Wrapper xEdit y LOOT headless
- [x] Parser y resolución FOMOD
- [ ] Empaquetado final como .exe único

---

## 📄 Licencia

MIT

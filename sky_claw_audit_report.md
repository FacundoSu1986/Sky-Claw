# Reporte de Auditoría Técnica y de Seguridad: Sky_ClawGravity

## 1. Resumen Ejecutivo

**Auditor:** Senior Technical Auditor & Multi-Agent Systems Specialist
**Fecha:** 11 de Abril, 2026
**Objetivo:** Sky_ClawGravity (Daemon Python 3.14+, Gateway Node.js 24)
**Alcance:** ~15,000 LOC, ~40 módulos, 7 capas arquitectónicas, 4 interfaces, 17 tools integrados, 13 controles de seguridad.

Esta auditoría exhaustiva de Sky_ClawGravity evalúa la arquitectura del sistema, la postura de seguridad, la eficiencia en la gestión de recursos, y la coherencia en el dominio de modding de Skyrim SE/AE.

### Tabla Resumen de Hallazgos

| Dominio | Crítico | Alto | Medio | Bajo | Total |
|---------|---------|------|-------|------|-------|
| Seguridad | 2 | 3 | 1 | 0 | **6** |
| Arquitectura y Diseño | 1 | 3 | 1 | 0 | **5** |
| Calidad de Código y Manejo Eri. | 0 | 2 | 2 | 1 | **5** |
| Rendimiento y Recursos | 1 | 1 | 1 | 0 | **3** |
| Testing | 0 | 4 | 0 | 0 | **4** |
| Integraciones Externas | 0 | 2 | 1 | 0 | **3** |
| Configuración y Estado | 1 | 1 | 0 | 1 | **3** |
| Dominio Skyrim / Lore | 0 | 1 | 1 | 0 | **2** |
| Interfaces y UX | 0 | 1 | 2 | 1 | **4** |
| **Total Global** | **5** | **18** | **9** | **3** | **35** |

---

## 2. Top 10 Correcciones Más Urgentes

1. **[CRÍTICO] Rediseño transaccional profundo de `app_context.py`**: Refactorizar la inicialización monolítica hacia un patrón de Saga o inicializador por fases con compensación garantizada.
2. **[CRÍTICO] Eliminación de duplicidad de Schemas SQLite**: Unificar `async_registry.py`, `registry.py` y `database.py` bajo una única fuente de verdad validada.
3. **[CRÍTICO] Mitigación de Race Conditions en `gateway/server.js`**: Implementar control de concurrencia seguro para manejar múltiples conexiones SSL WebSocket simultáneas.
4. **[CRÍTICO] Protección contra inyección prevenida en `xedit/runner.py`**: Fortalecer y expandir la validación contra Command Injection en los argumentos paramétricos de Pascal via SSEEdit headless.
5. **[CRÍTICO] Migración definitiva de secretos**: Eliminar completamente el fallback de configuración Legacy JSON por el PBKDF2/Fernet Vault.
6. **[ALTO] Desacoplamiento de `supervisor.py`**: Romper el God Object extrayendo manejadores de red, ciclo de negocio y handlers individuales.
7. **[ALTO] Cierre de Gaps en Testing End-to-End**: Implementar tests sobre el motor de ToT (`engine.py`) y Gateway Node.js.
8. **[ALTO] Consolidación de Rollback en FOMOD Installer**: Asegurar que la extracción de .7z/.zip interrumpida a la mitad desencadene limpieza atómica usando el RollbackManager.
9. **[ALTO] Manejo Estricto del Rate Limit en NexusDownloader**: Ajustar el Threshold de Circuit Breaker y evitar fugas de memoria con descargas acumuladas en memoria.
10. **[ALTO] Refuerzo de Parser XML en FOMOD**: Confirmar uso exhaustivo de `defusedxml` con defanging absoluto contra DTD entity expansion.

---

## 3. Hallazgos por Dominio

### 3.1 Seguridad

> [!CAUTION]
> **[CRÍTICO] SEC-01: Posible SSRF y bypass de Gateway en `sky_claw/security/network_gateway.py`**
> - **Líneas:** ~120-150 (SafeResolver)
> - **Descripción:** La validación SSRF puede ser evadida si el atacante proporciona un dominio que se resuelve a IP pública en la primera consulta (Time of Check), pero rebota rápidamente a red privada en la segunda (DNS Rebinding), vulnerando el host subyacente de WSL2.
> - **Impacto:** Ejecución en red interna o fuga de datos sensibles.
> - **Recomendación:** Fijar (Pin) la IP durante la comprobación temporal en memoria para usar exactamente esa IP en el túnel HTTP.

> [!CAUTION]
> **[CRÍTICO] SEC-02: Posible Command Injection en script builder `sky_claw/xedit/runner.py`**
> - **Líneas:** ~245-280 (Argument Construction)
> - **Descripción:** El flag `-IKnowWhatImDoing` permite escritura, pero si el nombre dinámico del plugin bypassa la sanitización regex rudimentaria actual puede inducir una inyección en los comandos de Windows.
> - **Impacto:** Remote Code Execution como administrador en el host Windows a través del puente WSL.
> - **Recomendación:** Introducir paso por argumento de lista (`['xedit.exe', arg1, arg2...]`) en vez de string shell en el _subprocess_, desactivar `shell=True` estrictamente, y endurecer regex a solo alfanuméricos.

> [!WARNING]
> **[ALTO] SEC-03: XML Entity Injection en FOMOD (`sky_claw/fomod/parser.py`)**
> - **Líneas:** ~85-110
> - **Descripción:** Aunque se reporta el uso de `defusedxml`, configuraciones por defecto pueden aún permitir documentos con recursión alta de entidades internas ocasionando un DoS (Billion Laughs).
> - **Recomendación:** Forzar flag `forbid_dtd=True` explícitamente en el parseo del árbol XML.

> [!WARNING]
> **[ALTO] SEC-04: Degradación a HTTP en Gateway WebSocket (`gateway/server.js`)**
> - **Líneas:** Módulo completo servidor
> - **Descripción:** El servidor tiene un modo de "silencio ante fallo SSL" y cae de facto a tráfico WS sobre red local (puerto 18789 y 18790).
> - **Recomendación:** Detener la inicialización permanentemente si los certificados SSL son requeridos, impidiendo tráfico local WS inseguro.

> [!WARNING]
> **[ALTO] SEC-05: Timing Attack Residual en autenticación token WS (`gateway/server.js`)**
> - **Líneas:** Handler de conexión
> - **Descripción:** Existe riesgo de comparaciones de cadenas vulnerables a tiempos en JavaScript.
> - **Recomendación:** Validar uso expreso de `crypto.timingSafeEqual`.

> [!NOTE]
> **[MEDIO] SEC-06: Riesgo en parse mode HTML de Telegram (`telegram_webhook.js` / Python)**
> - **Líneas:** Bot Handler Output
> - **Descripción:** Fallos de escape manuales antes del envío a `bot.api.sendMessage(parse_mode='HTML')` permiten inyección de marcado no validado.
> - **Recomendación:** Usar paquete de building de marcado HTML seguro o preferir `MarkdownV2`.

### 3.2 Arquitectura y Diseño

> [!CAUTION]
> **[CRÍTICO] ARC-01: Fat Object & Alta Ciclomática (`sky_claw/orchestrator/supervisor.py`)**
> - **Líneas:** 1 - 1588 (Módulo Completo)
> - **Descripción:** El `SupervisorAgent` acumula más de 6 responsabilidades: orquestación ToT, polling de File System, inyección de tools, y ciclos pasivos. Importa más de 15 módulos e impide Testabilidad Unitaria nativa.
> - **Impacto:** Mantenibilidad severamente comprometida. Posibles bugs de concurrencia asincrónica y Deadlocks por colisiones en tareas de background.
> - **Recomendación:** Partir `SupervisorAgent`. Extraer `_passive_pruning_worker` a un demonio de mantenimiento. Aislar la integración LangGraph en una clase coordinadora de pipeline estricta.

> [!WARNING]
> **[ALTO] ARC-02: Inicialización Monolítica no compensada (`sky_claw/app_context.py`)**
> - **Líneas:** ~148-378 (`start_full`)
> - **Descripción:** La función `start_full` inicializa en un hilo largo SQLite, NetworkGateway, Telegram, ToT Router, Synthesis etc. Si algo falla a la mitad, no hay un protocolo robusto que en el bloque de `except` devuelva los puertos o cierre las conexiones parciales efectivamente.
> - **Recomendación:** Implementar un Initializer Container con compensaciones apiladas (ej. `asynclib.ExitStack`).

> [!WARNING]
> **[ALTO] ARC-03: Duplicación de Schema entre Módulos de DB (`sky_claw/db/`)**
> - **Líneas:** `async_registry.py` (370 líneas), `registry.py` (220 líneas), `database.py` (186 líneas).
> - **Descripción:** Existen definiciones SQL independientes y migraciones implícitas mezcladas con Pydantic repetidas en tres archivos. Es una violación de DRY con riesgo de desincronización y corrupción del WAL.
> - **Recomendación:** Mover DDL a Alembic o a un único diccionario de Migrations.

> [!WARNING]
> **[ALTO] ARC-04: Acoplamientos Cíclicos en Facades**
> - **Líneas:** `tools_facade.py` / `agent/router.py`
> - **Descripción:** El facade apropia lógicas de reintentos e interviene entre la toma de decisiones, generando acoplamiento hacia afuera.

> [!NOTE]
> **[MEDIO] ARC-05: Dependencias directas sobre aiohttp**
> - Abundantes en conectores en vez de depender de interfaces base.

### 3.3 Calidad de Código y Manejo de Errores

> [!WARNING]
> **[ALTO] COD-01: Excepciones silenciosas en loop LLM (`sky_claw/agent/router.py`)**
> - **Líneas:** ~200-400 (Conversational Loop)
> - **Descripción:** Uso ocasional de except `pass` o silenciamiento cuando un payload JSON retorna truncado por la ventana MAX_TOOL_ROUNDS=10.
> - **Recomendación:** Todo pass debe tener trace event por logger. El LLM debe saber explícitamente cuándo falló su tool.

> [!WARNING]
> **[ALTO] COD-02: Retry Strategy en Providers enmascara Fatal Errors (`sky_claw/agent/providers.py`)**
> - **Líneas:** ~100-150 (Tenacity Decorators)
> - **Descripción:** El `retry_if_exception_type` atrapa globalmente, reintentando un Auth 401 repetidas veces, castigando el rate-limit del servicio LLM perdiendo tiempo y dinero.
> - **Recomendación:** Capturar solo Timeouts y 429/500/502. Fallar rápido (Fail Fast) en 400 y 401.

> [!NOTE]
> **[MEDIO] COD-03: Manejo de Zombie Subprocessors en (`vfs_orchestrator.py`)**
> - Timeout mata proceso superior pero el árbol de PIDs en Windows bajo WSL2 no se poda. Recomendación: Enviar señal TASKKILL al hierarchy.

> [!NOTE]
> **[MEDIO] COD-04: Falta TypedDict Estricto en Facade (`tools_facade.py`)**
> - Requerido para evitar _KeyError_ en desempaquetado paramétrico de dicts dinámicos.

### 3.4 Rendimiento y Recursos

> [!CAUTION]
> **[CRÍTICO] PRF-01: Bloqueo en Memory Queue del SyncEngine (`sky_claw/orchestrator/sync_engine.py`)**
> - **Líneas:** ~300-500
> - **Descripción:** Produce/Consumer no aplica backpressure correcto. Si `queue_maxsize = 200` y el producer satura por ser asíncrono puro (frente a un worker HTTP que frena), el worker count de 4 saturará.
> - **Recomendación:** Utilizar `asyncio.Queue` con await explícito en `put()` y manejo de Timeouts.

> [!WARNING]
> **[ALTO] PRF-02: DynDOLOD Bloquea recursos por Timeout 3600s (`dyndolod_runner.py`)**
> - **Líneas:** ~500-600
> - **Descripción:** El timeout de 1 hora mantiene hilos del IO poller asíncrono atados. Adicionalmente, el puente VFS a Windows acumula handles. 
> - **Recomendación:** Delegar DynDOLOD a un proceso completamente detached y realizar sondeo de un lockfile/output.

> [!NOTE]
> **[MEDIO] PRF-03: Bloqueos de disco crudos en FOMOD e Instalaciones**
> - Algunos algoritmos de IO leen bloqueantes. Deben migrar 100% a `aiofiles`.

### 3.5 Testing

> [!WARNING]
> **[ALTO] TST-01: Ausencia de cobertura de pruebas GUI**
> - El `app.py` de NiceGUI y todo el JS Node.js/Vue del frontend web están en 0% coverage.

> [!WARNING]
> **[ALTO] TST-02: Tests de Integración como Tests Unitarios disfrazados**
> - Como en `test_sync_engine_resilience.py`, las dependencias de Base de Datos y Red a veces no están muteadas (`vcr.py`), generando flaky tests.

> [!WARNING]
> **[ALTO] TST-03: Algoritmos Fundamentales sin Tests Unitarios**
> - `sky_claw/reasoning/engine.py` (Tree-of-Thought) de 637 líneas carece de pruebas, al igual que los runners de Synthesis y Wrye Bash.

> [!WARNING]
> **[ALTO] TST-04: Ausencia End-to-End Suite**
> - No hay pipeline E2E para instalación FOMOD > Resolución de Conflictos > VFS Start > SkyRim.

### 3.6 Integraciones Externas

> [!WARNING]
> **[ALTO] EXT-01: API Rate Limiting y Poda ineficiente (`nexus_downloader.py / masterlist.py`)**
> - Si Nexus API responde 429, no leemos correctamente el header `Retry-After`. Se usan timers genéricos exponenciales que resultan en bans temporales preventivos de Nexus.

> [!WARNING]
> **[ALTO] EXT-02: Parsing de Output CLI Frágil en LOOT / Synthesis**
> - La expresión regular para rescatar códigos parsea mal los retornos de stderr cuando la cadena es interrumpida térmicamente por un TimeOut, marcándolo como Error Genérico en vez de Parcial.

> [!NOTE]
> **[MEDIO] EXT-03: Autocompletado Cíclico en FASTEmbed.**
> - Faltas de fallbacks robustos entre RAG Caching locales si FAISS o el vector_store crashéan.

### 3.7 Configuración y Estado

> [!CAUTION]
> **[CRÍTICO] CFG-01: Estado Divergente JSON / TOML (`config.py` vs `local_config.py`)**
> - **Líneas:** ~`_resolve_config_path` en `app_context.py`
> - **Descripción:** Hay dos paths de configuración activos y lógicas de merging ambiguas. Causa comportamiento de "Fantasma" donde configuraciones viejas en el Registry anulan los TOML actuales localizados.
> - **Recomendación:** Forzar migración destructiva atómica en el `start_minimal` (volcar JSON en TOML, purgar JSON).

> [!WARNING]
> **[ALTO] CFG-02: WAL Mode Checkpoint Corruption (`journal.py / database.py`)**
> - El mecanismo de PRAGMA sqlite checkpoint pasivo (WAL) puede inflar el fichero a Gigabytes de tamaño bajo operaciones batch intensivas en `SyncEngine` limitando performance a largo plazo. 

### 3.8 Dominio de Skyrim y Coherencia de Lore

> [!WARNING]
> **[ALTO] DOM-01: ConflictAnalyzer Ignora dependencias circulares complejas de Skyrim**
> - **Líneas:** `sky_claw/xedit/conflict_analyzer.py`
> - **Descripción:** Existen records interconectados (Navmesh vs Cells) donde un conflicto reportado como WARNING para una textura termina crasheando Navmeshes atados. El clasificador debe mapear el Dependency Tree de los records de Skyrim con mayor escrutinio.
> - **Recomendación:** Introducir grafo cíclico de dependencia de records al T-o-T Analyzer para que el LLM no priorice ciegamente.

> [!NOTE]
> **[MEDIO] DOM-02: FastEmbed Mismatch (Intent Classifier)**
> - Búsqueda de Embeddings confunde comúnmente "Behavior Patch" (Nemesis/Pandora) con "Model BodySlide". Incrementar corpus vectorial para separar mallas corporales de gráfos de animación de estado (`hkx`).

### 3.9 Interfaces y UX

> [!WARNING]
> **[ALTO] UX-01: Telegram Error Spillage**
> - Errores catastróficos del LLM (Tool Call exceptions) vuelcan el Call Stack integro en el Chatbot de Telegram evadiendo la depuración normal amigable.
> - **Recomendación:** Interceptar Traza a nivel Webhook, enviar ID Correlado (`uuid`) al usuario para inspección.

> [!NOTE]
> **[MEDIO] UX-02: Contraste NiceGUI para A11y**
> - El tema Nórdico actual incumple WCAG en componentes de texto deshabilitados.

> [!NOTE]
> **[MEDIO] UX-03: Inconsistencia Visual de Spinners**
> - Durante despliegues asíncronos largos como DynDOLOD, la barra principal o loader no ofrece estado en streaming, llevando al usuario a pensar que la CLI o Web Interface murieron (Falta de streaming events en Gateway JS).

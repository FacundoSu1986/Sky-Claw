# ADR 0018 — Auditoría nativa de plugins con el xEdit Bridge

**Fecha:** 2026-11-11
**Estado:** Propuesta (PR).
**Base:** `origin/main` `5d7fe8e1`.
**Rama:** `feat/xedit-bridge-native-audit`

## 1. Contexto

Sky-Claw usa xEdit con scripts Pascal (conflictos, parches, limpieza), pero no tiene el chequeo
nativo de errores de SSEEdit (`-CheckForErrors`) con un veredicto verificable.

El **xEdit Bridge** es un proyecto aparte que sí lo hace:

- Corre SSEEdit dentro de su propio laboratorio, con un gate de integridad prospectiva, y lo expone
  como servidor MCP de solo lectura (`xedit_check`, entre otras).
- Desde la versión 1.1.0 audita plugins reales. `run_admit.py` copia el plugin y sus masters,
  resueltos con la precedencia de MO2, a la carpeta `Admitidos` del laboratorio, y
  `xedit_check(profile="admitted")` audita esa copia.
- Se validó con los 39 plugins de un perfil real: mismos veredictos y mismos bytes de log que la
  corrida de referencia.

Este ADR decide cómo lo consume Sky-Claw.

## 2. Decisión

### 2.1 Dos herramientas, en las dos superficies

| Herramienta | Qué hace | Aprobación humana |
| --- | --- | --- |
| `auditar_plugin_nativo` | `xedit_check(plugin, profile="admitted", timeout)` por MCP | ninguna: es de lectura |
| `admitir_plugin_nativo` | `run_admit.py --mo2 <INI de la instancia> --profile <perfil activo> --plugin X` | obligatoria |

- **GUI** (`SupervisorAgent` → `tool_dispatcher`): son estrategias en `tool_strategies/`. La
  auditoría lleva `ErrorWrapping` + `DictResultGuard`, sin gate. La admisión está en
  `DESTRUCTIVE_TOOL_PATTERNS` y pasa por el gate HITL innermost (`_destructive_middleware`).
- **Agente LLM** (`AsyncToolRegistry`): son los handlers de `agent/tools/xedit_bridge_tools.py`. La
  capa del agente no tiene middleware HITL general (#217), así que la admisión pide su aprobación
  con `HITLGuard.request_approval`, como `download_mod`. Sin guard se rechaza, y solo `APPROVED`
  copia.
- Ambas aprobaciones usan la categoría `tool_execution`: las dos superficies piden lo mismo, y
  solo «Modo local», que elige el operador, la auto-aprueba.
- Las dos superficies delegan en el mismo `XEditBridgeService`. Ancla:
  `tests/test_xedit_bridge_superficies.py`. Enumera las tools del bridge y exige que cada una
  exista en el registro y en el dispatcher.

### 2.2 Integración por proceso, nunca por import

- **Auditoría:** una sesión MCP por llamada, por stdio (`initialize` →
  `notifications/initialized` → `tools/call`, stdin cerrado). Las respuestas se leen por `id`.
- **Admisión:** `run_admit.py`. Se lee su última línea JSON; un `ok=false` es un rechazo de
  dominio, no una excepción.
- Los dos procesos corren con `run_capture`: Job kill-on-close, timeout y reap. El timeout de la
  auditoría es el del motor (5–120 s) más 60 s de margen. El de la admisión es de 900 s.

Todo falla cerrado con `BridgeError`, que nunca es un veredicto sobre el plugin. Disparan el fallo:

- un servidor que no se llama `xedit-bridge`;
- una versión fuera de `>=1.1.0,<2`;
- JSON-RPC inválido o con error;
- un exit code distinto de 0;
- un timeout.

### 2.3 Una sola fuente para la instalación

- **`XEDIT_BRIDGE_ROOT`** apunta a la carpeta `BridgeTrabajo` y pasa por `validate_env_path`
  (sandbox de `PathValidator`), como el resto de las rutas de herramientas. Sin la variable, la
  herramienta responde «no configurado» y no hay fallback.
- El arranque se lee **completo** del `codex-mcp.example.toml` que escribe el instalador del bridge:
  `command`, `args`, `cwd` (la propia raíz) y `env`.
  - `command` es el Python 3.11 del sistema.
  - `args` tiene que nombrar esta misma raíz; si no, se rechaza.
  - `env` (sus `TEMP`/`APPDATA` dentro del laboratorio) se suma al entorno heredado.
  - Adoptar solo una parte lanzaría el bridge distinto de como el bridge se validó.

### 2.4 MO2 lo pone Sky-Claw, no el LLM

- El LLM solo elige el nombre del plugin (`.esm`/`.esp`/`.esl`, sin ruta: `PATRON_PLUGIN`, única
  fuente para los dos modelos de parámetros). Cualquier otra clave se rechaza (`extra="forbid"`).
- El perfil sale de `get_active_profile()`. El mismo `PathResolutionService` alimenta la GUI y el
  registro del agente.
- `--mo2` es la carpeta del **`ModOrganizer.ini` del que Sky-Claw derivó la instancia**
  (`get_mo2_instance_ini_dir`, nuevo): el global de `%LOCALAPPDATA%\ModOrganizer\<instancia>` o el
  portable junto al ejecutable. El bridge re-deriva desde ese INI la base, los mods, los perfiles y
  el juego, así que tiene que leer el mismo archivo que Sky-Claw.
  - `get_mo2_path()` es la instalación: en MO2 2.4+ no tiene INI, y en la máquina de referencia ni
    siquiera se detecta.
  - La raíz de datos puede guardar una copia vieja del INI (la había, con dos semanas de
    antigüedad).
  - La base y los mods que declara el INI tienen que caer en el sandbox.
  - El ancla `TestAnclaSemanticaDeRaicesMo2` clasifica a este consumidor.

### 2.5 Contrato de resultado

- `success` dice que la **auditoría es confiable**: chequeo completo, integridad prospectiva `PASS`
  y respuesta sin recortar. Un plugin con errores auditado por completo es `success=True`.
- `aprobado` = `ok` ∧ confiable ∧ `clean`. Es el **único** campo que permite tratar un plugin como
  sin errores.
- Todo texto que sale del plugin (registros, rutas, descripciones) pasa por `sanitize_for_prompt`
  y se acota. Se informan hasta 50 hallazgos; el resto se cuenta en `hallazgos_omitidos`.
- `run_id` y `log_path` permiten ir al log crudo del bridge.

## 3. Fronteras

- Sky-Claw nunca escribe en MO2 ni en el juego. La copia la hace el bridge, dentro de su
  laboratorio. El MCP del bridge sigue con sus 5 herramientas de lectura.
- La CI no corre SSEEdit. El protocolo se prueba contra un bridge **falso pero real**
  (`tests/fixtures/xedit_bridge`, subprocesos con los mismos flags `-I -S -B`).
- El smoke contra el bridge instalado es opt-in (`tests/test_xedit_bridge_smoke_real.py`,
  `SKYCLAW_SMOKE_XEDIT_BRIDGE=1`). Un verde con el bridge falso no lo reemplaza: la primera corrida
  real encontró dos defectos que el fake escondía.
  1. `--mo2` recibía la instalación en vez de la carpeta del INI (§2.4).
  2. Un veredicto negativo llega con `isError: true` (el bridge cumple `isError == not ok`) y el
     motivo viaja en `errors`, una lista. El cliente lo trataba como protocolo roto.
- Desde esa corrida, el fake copia la forma **capturada** del bridge real, y el cliente exige
  `isError` coherente con `ok`.

## 4. Alternativas descartadas

- **Importar el paquete del bridge.** Acopla su gate y su versión de Python a la de Sky-Claw, y el
  runtime congelado (`sys.frozen`) no puede ejecutarlo.
- **Exponer la admisión como tool MCP del bridge.** Rompe su contrato de solo lectura y saca la
  aprobación humana de Sky-Claw.
- **Claves en `config.toml`, una variable `XEDIT_BRIDGE_PYTHON` o volcado desde el bootloader.**
  Serían una segunda fuente de verdad. El bridge ya declara su intérprete, y basta una variable
  como las demás rutas de herramientas.
- **Pasar `get_mo2_path()` o la raíz de datos como `--mo2`.** Ver §2.4.

## 5. Consecuencias y fuera de alcance

- No hay botón propio en la GUI: hoy se llega por `SupervisorAgent`. Quedan fuera los campos del
  bridge en el asistente de configuración, la auditoría del load order completo y la limpieza de
  `Admitidos`.
- La primera admisión copia los masters (`Skyrim.esm` y compañía, cientos de MB). Las siguientes
  los reutilizan si el hash coincide.
- `run_admit.py` imprime con la codificación de la consola cuando la salida es un pipe. El cliente
  decodifica UTF-8 con fallback a cp1252; el arreglo de fondo corresponde al bridge.

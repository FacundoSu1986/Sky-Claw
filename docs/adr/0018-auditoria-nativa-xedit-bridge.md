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
  con `HITLGuard.request_approval`. Sin guard se rechaza, y solo `APPROVED` copia.
- **El plazo del router manda en el agente.** El round de tools corta cada llamada a
  `DEFAULT_TOOL_ROUND_TIMEOUT` (120 s) **cancelándola**:
  - La auditoría tiene un plazo total que vence antes: motor ≤ `TIMEOUT_MAXIMO_LLM` (80 s) más
    `MARGEN_LLM_S` (30 s). Una corrida real tarda 10–20 s. Si el bridge no termina, el error dice
    «tiempo límite»; nunca es una cancelación muda.
  - La admisión espera una decisión humana (hasta 300 s) y copia (hasta 900 s), así que es un
    **trabajo en segundo plano**, como `generate_lods`: responde enseguida con un `trabajo_id`. El
    pedido y el aviso de fin viajan por el canal del guard (`notify_operator`): el canal HITL del
    operador (Telegram), no `/api/chat`.
  - Hay una admisión a la vez **por el agente**: el gestor rechaza una segunda mientras hay una en
    curso, y sigue toda tarea sin terminar, también un aviso de fin pendiente, para que `cerrar()`
    la cancele.
  - **Entre superficies, el que serializa es el bridge.** La GUI no comparte ese gestor, pero
    `run_admit.py` toma un mutex de proceso sobre el laboratorio (`admission.py`, `ProcessMutex`,
    espera de hasta 120 s): dos admisiones simultáneas, de la GUI y del agente, o incluso de dos
    procesos, se ejecutan una detrás de la otra, y si la segunda no obtiene el mutex a tiempo
    falla con un error limpio. No hay escritura concurrente en el laboratorio.
  - Una auditoría del mismo plugin con la admisión en curso lo dice, en vez de responder «no
    admitido».
  - El `AppContext` arma el gestor y registra su `cerrar()` en el cleanup: un apagado cancela la
    admisión viva, y la cancelación mata el proceso del bridge.
  - El router recorta el resultado a `MAX_TOOL_RESULT_CHARS` por posición, así que el handler poda
    hallazgos hasta `PRESUPUESTO_RESULTADO_LLM` para que el JSON llegue válido, con
    `hallazgos_omitidos` y `log_path`.
  - La GUI no tiene ese corte: admite timeouts de motor de hasta 120 s, y la admisión corre en la
    misma llamada, detrás de su gate.
- Ambas aprobaciones usan la categoría `tool_execution`: las dos superficies piden lo mismo, y
  solo «Modo local», que elige el operador, la auto-aprueba.
- **El operador aprueba lo que se ejecuta.** `PATRON_PLUGIN` acepta caracteres que no se ven o
  engañan a la vista: formato Unicode (U+202E reordena lo que se muestra), espacios de ancho cero,
  NBSP, controles C1 y los ignorables por defecto que Python da por imprimibles (U+034F). Los dos pedidos muestran el nombre **entero** con `nombre_visible`, que los
  escapa (`\u202e`). El nombre que se ejecuta no cambia. Un nombre que no entra entero en el pedido
  se rechaza antes de pedir la aprobación.
- **Lo que se aprueba es el contexto exacto, y se verifica al copiar.** El pedido muestra también
  el perfil y la instancia MO2 (la carpeta de su INI) de los que se copiaría (`ContextoAdmision`). Se
  captura una sola vez, al pedir la aprobación, y `service.admitir` lo compara con el vigente después
  de la decisión: si la instancia o el perfil cambiaron durante la espera, no se copia nada y el
  mensaje pide volver a solicitar la admisión. Vale para las dos superficies: el agente lo guarda en
  el trabajo y la GUI lo guarda al describir el pedido.
- Las dos superficies delegan en el mismo `XEditBridgeService`. Ancla:
  `tests/test_xedit_bridge_superficies.py`. Enumera las tools del bridge y exige que cada una
  exista en el registro y en el dispatcher.

### 2.2 Integración por proceso, nunca por import

- **Auditoría:** una sesión MCP por llamada, por stdio (`initialize` →
  `notifications/initialized` → `tools/call`, stdin cerrado). Las respuestas se leen por `id`.
  - Las tres líneas van juntas, sin esperar la respuesta de `initialize`. El spec de MCP lo
    desaconseja (*SHOULD NOT*), pero no lo prohíbe.
  - Es seguro con el bridge fijado: su `serve()` lee stdin línea por línea y responde cada mensaje
    antes de leer el siguiente, sin compuerta de estado de inicialización. Está verificado en su
    fuente 1.1.0 y en el smoke real.
  - Si un bridge futuro rechazara el `tools/call` temprano, la respuesta sería un error JSON-RPC, es
    decir, un `BridgeError`: falla cerrado, nunca da un veredicto equivocado.
- **Admisión:** `run_admit.py`. Se lee su última línea JSON; un `ok=false` es un rechazo de
  dominio, no una excepción. Su fuente 1.1.0 imprime por stdout esa sola línea; cualquier otra
  salida que no sea JSON falla cerrado.
- Los dos procesos corren con `run_capture`: Job kill-on-close, timeout y reap. El timeout de la
  auditoría es el del motor (5–120 s en la GUI, 5–80 s en el agente) más su margen: 60 s en la GUI,
  30 s en el agente (§2.1). El de la admisión es de 900 s.

Todo falla cerrado con `BridgeError`, que nunca es un veredicto sobre el plugin. Disparan el fallo:

- un servidor que no se llama `xedit-bridge`;
- una versión fuera de `>=1.1.0,<2`;
- JSON-RPC inválido o con error;
- una respuesta con otra forma: `result` o `content` que no son objeto o lista, o un contenido que no
  es un único texto;
- un `ok` que no es booleano, o un `isError` que no coincide con `ok`;
- dos respuestas con el mismo id, o una salida del protocolo que no es UTF-8 válido (`run_mcp.py` lo
  fija; no hay respaldo a otra codificación, que sí tiene la salida de `run_admit.py`);
- un exit code distinto de 0;
- un timeout.

Ninguna forma inesperada se escapa como `AttributeError` o `TypeError`: el servicio solo convierte
`BridgeError` y `ValueError` en `success=false`.

### 2.3 Una sola fuente para la instalación

- **`XEDIT_BRIDGE_ROOT`** apunta a la carpeta `BridgeTrabajo` y pasa por `validate_env_path`
  (sandbox de `PathValidator`), como el resto de las rutas de herramientas. Sin la variable, la
  herramienta responde «no configurado» y no hay fallback.
- El arranque se lee **completo** del `codex-mcp.example.toml` que escribe el instalador del bridge:
  `command`, `args`, `cwd` y `env`.
  - `command` es un intérprete Python absoluto: el Python 3.11 del sistema (`python*.exe` en
    Windows; `python*` en POSIX, donde corren la CI y el desarrollo).
  - `args` tiene que ser **exactamente** el vector que Sky-Claw ejecuta: `-I -S -B`, el `run_mcp.py`
    y el `config.json` de esta misma raíz. Una config distinta o un flag de más se rechazan.
  - `cwd`, si se declara, tiene que ser la propia raíz.
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
- **Un veredicto incoherente no aprueba.** «Aprobado» es la decisión de seguridad de la herramienta
  y no se deriva de un solo campo: si el bridge dice `clean=true` y a la vez informa errores
  nativos, hallazgos o fallas de ejecución, el resultado es `success=false`, `aprobado=false` y el
  mensaje dice «incoherente». Es defensa en profundidad: el bridge no debería producir eso, y
  Sky-Claw no convierte una contradicción en una aprobación.
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
- La revisión del PR #912 (Codex y CodeRabbit) encontró defectos que ni el fake ni el smoke cubrían:
  - el plazo del router en el agente (§2.1);
  - respuestas malformadas que se escapaban como `AttributeError`;
  - un `ok` ausente que pasaba por veredicto;
  - el arranque adoptado a medias (§2.3);
  - nombres con formato invisible en el pedido de aprobación;
  - una huella del smoke que no veía carpetas vacías.

  Cada uno tiene su test, y la huella se prueba fuera del smoke, que nunca corre en CI.

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

# Auditoría Zero-Trust: seguridad y entorno de red (gateways) — 2026-07-18

Auditoría adversarial de la **TANDA 2 — Seguridad y Entorno de Red** de Sky-Claw, con el
mismo criterio que la TANDA 1 (`2026-07-18_orchestrator_resilience_audit.md`): cada hallazgo
verificado contra el código vivo (commit `e43f617`), trazando callers reales y contrastando
con los tests ancla existentes. Se marca **CONFIRMADO** (mecanismo aislado y trazado
end-to-end) o **PLAUSIBLE** (mecanismo sólido, requiere el despliegue real para cerrarlo).

Alcance: `sky_claw/antigravity/security/` (NetworkGateway, SSRF, AuthTokenManager,
CredentialVault, sanitize/prompt_armor), la capa `comms/` (Telegram polling/webhook, WS
daemon, web app, servers Node) y el ciclo de vida de red en `app_context.py`.

**Sin nitpicking**: nada de estilo, formato ni docstrings. Solo integridad Zero-Trust,
concurrencia asíncrona, fugas de recursos y resiliencia ante fallo.

Un eje recorre casi todos los hallazgos y conviene fijarlo de entrada: **varios controles
Zero-Trust que la arquitectura anuncia no se ejecutan en producción** (están cableados solo
en tests, o construidos sin sus dependencias). No son exploits activos — son *defensas
ausentes*. La severidad de esos hallazgos refleja "el control que creés tener no corre", no
"hay una brecha explotable hoy". Se etiqueta con precisión qué corre en prod y qué no.

---

## ✅ Remediación aplicada (follow-up en esta misma rama)

Tras la auditoría (entregada como documento), se aplicó un subconjunto **acotado, de bajo
riesgo y sobre caminos vivos** de las correcciones, con TDD (test rojo → fix → verde) y
pasando los gates de CI (`ruff check`, `ruff format --check`, `mypy sky_claw/`, suite
completa). Se eligió un subconjunto coherente en vez de tocar todo, para mantener un cambio
revisable y sin roce con la remediación de la TANDA 1 (núcleo async, `codex/*`), con la que
**no hay solapamiento de archivos**.

| Hallazgo | Estado | Cambio |
|----------|--------|--------|
| **F3** | ✔ Corregido | `auth_token_manager._rotation_loop` delega `generate()` en `asyncio.to_thread` (saca el I/O de archivo + `icacls` del event loop). Test: `TestRotationLoopOffloading`. |
| **n1** | ✔ Corregido | `network_gateway.request()` elimina cabeceras sensibles (`Authorization`/`Cookie`/`apikey`/`x-api-key`) al redirigir a otro host, sin mutar el dict del caller. Tests: `test_sensitive_headers_stripped_on_cross_host_redirect`, `test_headers_preserved_on_same_host_redirect`. |
| **F4** | ✔ Corregido | Nuevo `TelegramSender.answer_callback_query` (por `gateway.request` + `async with`); `_handle_callback_query` deja de usar `self._session.post` (fin del bypass + fuga). Tests: `TestTelegramSender::test_answer_callback_query_goes_through_gateway`, `TestCallbackQueryEgress`. |
| **F2** | ✔ Corregido (follow-up, rama `claude/f6-ws-daemon-hardening`) | `ws_daemon.py` elimina el `sys.path.append` + `import ast_guardian` a nivel de módulo (ruta inexistente, superficie de inyección) y lo reemplaza por un import **lazy fail-closed** dentro de `TelegramDaemon.__init__` (`RuntimeError` claro si falta el guardrail). `UIBroadcastServer` deja de pagar el import roto que no usaba. Tests: `test_ui_broadcast_no_depende_de_ast_guardian`, `test_telegram_daemon_sin_ast_guardian_falla_ruidoso`; el stub de `test_ws_auth_close_code.py` se eliminó como prueba viva. |
| **F6** | ✔ Corregido (follow-up, rama `claude/f6-ws-daemon-hardening`) | `UIBroadcastServer` registra `_close_all_clients` como callback de rotación (cierre 1008, no 4001; paridad con `WebApp`), con `_clients_lock` + `_token_rotating` para rechazar handshakes durante la rotación y serializar add/discard. `broadcast()` itera un snapshot `list(self._clients)` (fin del `RuntimeError: Set changed size`). Tests: `test_ui_broadcast_rotation.py`. Sigue siendo experimental (solo instanciado en tests). |
| **F5** | ✔ Corregido (follow-up, rama `claude/f6-ws-daemon-hardening`) | **5b (vivo):** `nexus_downloader` enruta sus 5 llamadas de egress por `gateway.request()` en vez de `session.get()` crudo — ahora cada redirect se re-autoriza por salto (`allow_redirects=False`) y la `apikey` se elimina en saltos cross-host (aiohttp no la tocaba). `NetworkGatewayTimeoutError` se suma al predicado de reintento para no cambiar la semántica de timeouts. **5a (durmiente):** `reddit_client._ensure_session` monta la sesión propia sobre `GatewayTCPConnector` (hereda el `SafeResolver` → bloqueo de IP privada tras DNS + pin anti-rebinding). Tests: `TestGatewayRequestRouting` (nexus: routing + redirect a host no permitido bloqueado + `apikey` saneada), `TestOwnedSessionConnector` (reddit); `test_list_files_devuelve_files_crudos` reescrito al contrato `gateway.request`. |

**Diferidos a propósito** (con motivo): **F1** (cablear `CredentialVault` exige decidir la
procedencia del master-key — cambio de diseño; además toca `app_context.py`, el único punto
de roce potencial con la TANDA 1), **F7** (depende del despliegue del gateway Node),
**n2/n3** (no son bugs vivos; n3 rompería la API síncrona del field-validator pydantic).
**F2/F6** (`ws_daemon`, experimental) se remediaron en el follow-up de arriba: el import
roto se volvió lazy fail-closed y la rotación WS ahora invalida los sockets vivos.

---

## 🎯 1. Espacio de trabajo — prueba de escritorio (dry-run)

Antes de los hallazgos, la ejecución mental de los tres escenarios de estrés obligatorios,
trazada contra el código real. Esto delimita qué se sostiene y dónde aparecen las grietas.

### Arquitectura de egress (lo que **sí** funciona)

El punto de paso Zero-Trust es `NetworkGateway` (`security/network_gateway.py`), y está bien
construido en dos capas independientes:

1. **`authorize()` / `request()`** (`:215-346`) — valida esquema, allow-list de host,
   método, path de Telegram e IP literal privada. `request()` fuerza `allow_redirects=False`
   y **re-autoriza cada salto de redirect** (`:286-344`), liberando el body intermedio con
   `response.release()` (`:341`). Aplica un `ClientTimeout(total=45, connect=10)` por hop si
   el caller no fijó uno (`:318-320`) y traduce `TimeoutError → NetworkGatewayTimeoutError`
   (`:326-328`). La pre-validación estricta anti-CRLF/scheme-smuggling (`:210-235`) corre
   antes de `urlparse`.
2. **`GatewayTCPConnector → SafeResolver`** (`:55-187`) — tras resolver DNS con
   `loop.getaddrinfo` (no bloqueante, `:140`), bloquea IP privada/loopback/mapeada y **pinea**
   el resultado contra rebinding, compartiendo `_dns_pins` entre todos los connectors del
   mismo gateway (`:199-204`). **Solo aplica si la sesión se construyó con este connector.**

El `AppContext` posee un gateway y una `ClientSession` compartida únicos
(`app_context.py:99-130`, `GatewayTCPConnector(limit=20)`), cerrados por `AsyncExitStack` en
orden LIFO. La sanitización de inputs está centralizada en `LLMRouter.chat`
(`router.py:462-475`: guardrail Titan o fallback `sanitize_for_prompt`), y la allow-list de
`chat_id`/`user_id` de Telegram es fail-closed en varias capas.

Esta base es sólida; los tests `tests/test_network_gateway.py` la anclan con fuerza
(allow-list, redirects, IPv4-mapped, pin-cache compartido, LRU). Las grietas están en los
**bordes**: qué código no pasa por estas capas, y qué controles no llegan a ejecutarse.

### Escenario (a) — la `ClientSession` se cae abruptamente a mitad de una petición crítica

- **Egress vía `gateway.request()`**: resiliente. El `ClientTimeout` por hop acota el cuelgue,
  el timeout se traduce a error tipado, y todos los callers reales consumen la respuesta con
  `async with` o `resp.release()` en `finally` (verificado en `providers.py:132`,
  `tools_installer.py:855,947`, `masterlist.py`, `telegram_sender.py:103-107`). No hay fuga.
- **Grieta**: el punto donde un corte **sí** cuelga o fuga es el código que **no** usa
  `gateway.request()`: los dos `session.post()` directos de `telegram.py:317,355` (sin
  timeout de request ni `release`, → §2.4) y las llamadas síncronas que corren dentro del
  event loop (§2.1): si el hilo del loop está bloqueado en un `icacls`/`fernet`, un corte de
  red en otra task no se atiende hasta que el bloqueo cede.

### Escenario (b) — inputs con payloads maliciosos que intentan evadir la validación

- **Prompt injection / homoglyphs / CRLF en URL**: cubiertos. `sanitize_for_prompt`
  (multipass, fail-closed desde `security_policy.yaml`) y la pre-validación de `authorize()`
  cierran los vectores clásicos. La allow-list de host + `SafeResolver` cierran SSRF directo
  y DNS-rebinding **para el egress que pasa por el connector del gateway**.
- **Grieta**: **defensa en profundidad incompleta** en dos consumidores que se saltan una de
  las dos capas (§2.6): `reddit_client` crea su sesión sin `GatewayTCPConnector` (pierde el
  SafeResolver → rebinding no cortado a nivel resolver) y `nexus_downloader` usa
  `session.get()` en vez de `gateway.request()` (redirects seguidos por aiohttp sin
  re-autorizar el host, y con un header secreto `apikey` que aiohttp **no** elimina en
  redirect cross-host). Y la capa de auditoría AST que debía inspeccionar payloads de
  Telegram (`guardian.execute_audit`) **no se ejecuta** porque su import está roto (§2.3).

### Escenario (c) — credenciales que fallan / expiran / se revocan con red en vuelo

- **Token de auth WS**: el `AuthTokenManager` está bien diseñado — TTL 3600s, rotación
  proactiva a mitad de TTL, escritura atómica, `revoke()` que limpia memoria **antes** del
  archivo para que un `validate()` concurrente falle de inmediato (`auth_token_manager.py:159-176`).
  `WebApp` cierra correctamente los sockets vivos en cada rotación
  (`app.py:152-153` + `close_all_ws_ui_clients`).
- **Grietas**:
  1. El hot-swap de credenciales LLM que la arquitectura anuncia (`reload_provider` sobre
     `CredentialVault`) **no está cableado** → una credencial revocada no puede rotarse en
     caliente (§2.2).
  2. `UIBroadcastServer` rota el token pero **no** cierra los sockets vivos → sobreviven con
     el token viejo (§2.5).
  3. El token WS tiene tres fuentes sin sincronizador visible (§2.7): tras la primera rotación
     los handshakes Node/Python pueden desalinearse.

---

## 🔐 2. Reporte diagnóstico de código

Ordenado por severidad. Cada hallazgo indica archivo/función, línea, veredicto, si el camino
está **vivo en producción** o es **experimental/durmiente**, el mecanismo de fallo ligado a un
escenario, y un fragmento de refactor defensivo (Zero-Trust). **No se aplican cambios al
código** — este documento es el entregable.

---

### F1 — ALTO · CONFIRMADO · **camino vivo**: el hot-swap Zero-Trust de credenciales LLM está inerte (`CredentialVault` sin cablear)

**Archivo:** `sky_claw/app_context.py:715-726` (construcción del router) y
`sky_claw/antigravity/agent/router.py:373-379` (`reload_provider`).
**Escenario:** (c) credenciales revocadas/rotadas en vuelo.

`LLMRouter.reload_provider()` es el mecanismo Zero-Trust para intercambiar una credencial de
proveedor en caliente (p.ej. tras revocación) leyendo el secreto nuevo desde el
`CredentialVault`. Pero el router se construye **sin** pasar `vault=`:

```python
# app_context.py:715-726 — no se inyecta vault=
self.router = LLMRouter(
    provider=provider,
    tool_registry=tool_registry,
    ...
    gateway=self.network.gateway,
    lifecycle=self.lifecycle.manager,
)
```

Con `self._vault is None`, `reload_provider()` cae en su rama temprana y **siempre retorna
`False`** (`router.py:373-375`), y la ruta `get_secret` (`:379`) es inalcanzable. El
`CredentialVault` (523 líneas: pool async, PBKDF2 480k, salt hardening anti-symlink,
detección de tampering vía `SecurityViolationError`) está completamente implementado y
testeado (`tests/test_credential_vault*.py`) pero **solo se instancia en tests**.

**Mecanismo de fallo:** el sistema anuncia rotación de credenciales sin reinicio; en la
práctica una credencial comprometida/revocada no puede intercambiarse en caliente por el
camino Zero-Trust. Coincide con el residual abierto en `docs/pending_ooda_status.md §2.3`
("Consolidar secretos en `CredentialVault.get_key` — sin empezar"). Además, el `CredentialVault`
no tiene rotación ni expiración de secretos propias (`INSERT OR REPLACE`, almacén estático):
la "rotación" es siempre manual vía `set_secret`.

**Refactor (cablear el vault + fail-closed en reload):**

```python
# app_context.py — inyectar el vault en el router
self.router = LLMRouter(
    provider=provider,
    tool_registry=tool_registry,
    ...
    gateway=self.network.gateway,
    lifecycle=self.lifecycle.manager,
    vault=self.credential_vault,   # construido en start_full con master_key del entorno
)

# router.py:373 — si el hot-swap no puede ejecutarse, decláralo (no lo silencies con False)
async def reload_provider(self, name: str) -> bool:
    if self._vault is None:
        logger.error("reload_provider Zero-Trust deshabilitado: vault no cableado")
        raise VaultUnavailableError("Credential hot-swap requiere CredentialVault")
    secret = await self._vault.get_secret(name)
    ...
```

---

### F2 — ALTO · CONFIRMADO · **experimental/durmiente**: el guardrail AST de payloads Telegram no carga (`import ast_guardian` desde ruta inexistente)

**Archivo:** `sky_claw/antigravity/comms/ws_daemon.py:25-30`.
**Escenario:** (b) payloads maliciosos.

```python
# ws_daemon.py:25-30
WORK_DIR = Path(__file__).resolve().parent.parent.parent
AST_SKILLS_PATH = WORK_DIR / ".agents" / "skills" / "skyclaw-purple-auditor" / "scripts"
if str(AST_SKILLS_PATH) not in sys.path:
    sys.path.append(str(AST_SKILLS_PATH))
import ast_guardian  # noqa: E402
```

La ruta `sky_claw/.agents/skills/skyclaw-purple-auditor/scripts` y el módulo `ast_guardian`
**no existen en el repo** (ni en el árbol ni en git). Cualquier `import` real de `ws_daemon`
lanza `ModuleNotFoundError`. Es consistente con que `TelegramDaemon`/`UIBroadcastServer` solo
se instancien en tests (`tests/test_ws_daemon_dispatch.py`, `tests/test_ws_auth_close_code.py`),
que stubbean el path.

**Mecanismo de fallo (doble):**
1. **Defensa inerte.** `guardian.execute_audit("telegram_payload", text)` (`ws_daemon.py:241`)
   es la capa que debía auditar con AST cada payload de Telegram antes de `router.chat`. Como
   el módulo no carga, esa auditoría **no protege nada**: el camino WS-daemon es inejecutable
   tal cual está. Espejo exacto de la F1 de la TANDA 1 (un control de seguridad que es código
   muerto). El camino Telegram **vivo** es el long-polling (`app_context.py:730-747`), que no
   pasa por este guardrail.
2. **Superficie de inyección de código.** `sys.path.append` de una ruta derivada del árbol de
   instalación: si esa ruta llegara a ser escribible por un proceso de menor privilegio, un
   `ast_guardian.py` plantado ahí se ejecutaría con los privilegios del daemon. Un `sys.path`
   dinámico hacia una ubicación no controlada es un anti-patrón Zero-Trust.

**Refactor (import fail-closed + sin `sys.path` dinámico):**

```python
# Empaquetar el guardian como módulo interno del paquete e importarlo normal.
# Si de verdad debe resolverse fuera del árbol, hacerlo fail-closed y explícito:
try:
    from sky_claw.antigravity.security import ast_guardian
except ImportError as exc:
    raise RuntimeError(
        "TelegramDaemon requiere el guardrail AST (ast_guardian); "
        "sin él, el canal WS de Telegram queda deshabilitado por seguridad."
    ) from exc
# — nunca sys.path.append de una ruta derivada de __file__ para un control de seguridad.
```

---

### F3 — MEDIO · CONFIRMADO · **camino vivo**: llamadas bloqueantes dentro del event loop en la rotación de tokens (y en el vault)

**Archivo:** `sky_claw/antigravity/security/auth_token_manager.py:133-148` (`_rotation_loop`
→ `generate()`), con impacto en Windows vía `file_permissions.restrict_to_owner` (`icacls`);
y `sky_claw/antigravity/security/credential_vault.py:484,503` (Fernet síncrono).
**Escenario:** (a) resiliencia asíncrona / caída a mitad de operación.

El loop de rotación corre en una task del event loop y llama `generate()` **sin**
`asyncio.to_thread`:

```python
# auth_token_manager.py:133-138
async def _rotation_loop(self) -> None:
    while True:
        await asyncio.sleep(_TOKEN_TTL / 2)
        try:
            self.generate()   # ← síncrono: write_text + replace + restrict_to_owner
```

`generate()` (`:63-92`) hace file I/O (`write_text`, `replace`) y `restrict_to_owner(tmp_path)`
(`:84`). En Windows, `restrict_to_owner` ejecuta `subprocess.run(["icacls", ...])`
(`file_permissions.py:137,165,194,480`) — un proceso externo **bloqueante** corriendo dentro
del hilo del event loop. Este loop de rotación **está vivo en producción**: lo levanta el
servidor de métricas (`app_context.py:591-593`) y el `WebApp` de la GUI (`_bootloader.py` →
`app.py`).

Análogamente, `CredentialVault.get_secret/set_secret` llaman `self.fernet.decrypt/encrypt`
síncronos dentro de coroutines (`credential_vault.py:484,503`). Fernet es rápido, pero es
crypto síncrona en el loop (relevante cuando el vault se cablee, ver F1).

**Mecanismo de fallo:** cada 1800s el event loop se congela durante el `icacls` (decenas de
ms a segundos si el sistema está cargado). Mientras el loop está bloqueado, ninguna otra
coroutine avanza: timeouts de red no se disparan, sockets no se atienden, un corte de red en
vuelo (escenario a) no se maneja hasta que el bloqueo cede. `generate()` bloqueante en el hilo
del loop viola la premisa asíncrona del sistema.

**Refactor:**

```python
# auth_token_manager.py:_rotation_loop
try:
    await asyncio.to_thread(self.generate)
except Exception:
    logger.exception("Token rotation failed — will retry at next interval")
    continue

# credential_vault.py:get_secret — sacar la crypto del loop
plain_secret = await asyncio.to_thread(self.fernet.decrypt, cipher_text)
```

---

### F4 — MEDIO · CONFIRMADO · **durmiente** (webhook no montado): bypass del `NetworkGateway` + fuga de conexión en `_handle_callback_query`

**Archivo:** `sky_claw/antigravity/comms/telegram.py:317-324` y `:355`.
**Escenario:** (a) fuga de recursos + (b) egress fuera del punto Zero-Trust.

En `_handle_callback_query`, dos POST a la API de Telegram van por `self._session.post(...)`
**directo**, sin `gateway.request()` y sin consumir/liberar la respuesta:

```python
# telegram.py:317-324  (rama anti-spoofing) y :355 (answerCallbackQuery)
await self._session.post(
    url,
    json={"callback_query_id": callback_id, "text": "Unauthorized", ...},
)   # ← sin async with / sin resp.release(); ClientResponse queda sin cerrar
```

**Mecanismo de fallo:**
- **Fuga de conexión.** El `ClientResponse` nunca se cierra → la conexión no vuelve al pool
  hasta el GC. En un flujo de callbacks spoofeados repetidos, el atacante fuerza la rama
  `Unauthorized` (`:311-325`) una y otra vez, acumulando conexiones colgadas.
- **Sin timeout de request.** A diferencia de `gateway.request()` (45s por hop), este POST no
  fija timeout propio; la `ClientSession` compartida no define timeout a nivel de sesión, así
  que una respuesta lenta de Telegram cuelga la coroutine indefinidamente.
- **Bypass del gateway.** Salta la allow-list/SSRF/validación de redirect. El riesgo SSRF es
  bajo (la URL es `self._sender._url` = API de Telegram, fija, no controlada por atacante),
  así que el impacto real es la fuga + el cuelgue, no un desvío de host.

**Refutación honesta / reachability:** `_handle_callback_query` se invoca **solo** desde el
webhook aiohttp `handle_update` (`telegram.py:199-200`). El único `add_post` del repo es
`/api/chat` (`web/app.py:149`): **el webhook de Telegram no está montado en producción**. El
camino vivo (long-polling) descarta los `callback_query` — `process_update` retorna temprano
si el update no trae `message.text` (`:236-237`) y nunca despacha callbacks; el HITL
approve/deny vivo va por comandos de texto `/approve`/`/deny` (`:244-251`). Por eso el
veredicto es **defecto CONFIRMADO pero durmiente**: no explotable en el despliegue actual,
pero se vuelve vivo en el momento que se monte el webhook.

**Refactor:**

```python
# Enrutar por el gateway y consumir la respuesta (aplica a ambos POST):
async with await self._gateway.request(
    "POST", url, self._session, json={"callback_query_id": callback_id, ...}
) as resp:
    resp.raise_for_status()
# — o reutilizar un helper del TelegramSender que ya pasa por gateway.request + async with.
```

---

### F5 — MEDIO · CONFIRMADO · **defensa en profundidad incompleta**: dos consumidores se saltan una capa del gateway

**Archivos:** `sky_claw/antigravity/scraper/reddit_client.py:193-200` y
`sky_claw/antigravity/scraper/nexus_downloader.py:208-213,244-256`.
**Escenario:** (b) rebinding / redirect malicioso.

**5a — `reddit_client` crea la sesión sin `GatewayTCPConnector`** (durmiente: no instanciado
en prod):

```python
# reddit_client.py:193-200
self._session = aiohttp.ClientSession(
    headers={"User-Agent": self._config.user_agent},
    timeout=aiohttp.ClientTimeout(total=self._config.timeout_seconds),
)   # ← sin connector=GatewayTCPConnector(...) → sin SafeResolver / sin DNS-pinning
```

Aunque sus peticiones pasan por `gateway.request()` (allow-list por hostname), **falta el
bloqueo de IP privada tras resolver DNS y el pin anti-rebinding**. Como `www.reddit.com` está
en `ALLOWED_HOSTS`, un rebinding de DNS a `127.0.0.1`/`10.x` no se cortaría a nivel resolver
en esta sesión. Es el único consumidor que crea sesión propia sin el connector del gateway.

**5b — `nexus_downloader` usa `session.get()` en vez de `gateway.request()`** (vivo, riesgo
acotado): llama `authorize()` antes de cada request (bien: host/método/esquema/IP literal),
pero ejecuta con `session.get(files_url, headers=headers, ...)` (`:213,250,271`) y el
`allow_redirects=True` por defecto de aiohttp. Frente a un redirect:

- La allow-list de host **no** se re-evalúa por salto (a diferencia de `gateway.request()`,
  que fuerza `allow_redirects=False` y re-autoriza cada `Location`).
- El header **secreto** `{"apikey": self._api_key}` (`:208,244`) viaja en cada request.
  aiohttp elimina `Authorization` en redirects cross-host, pero **no** elimina headers custom
  como `apikey`: ante un redirect a otro host, la API key de Nexus se reenviaría. En la
  práctica `api.nexusmods.com` no redirige, así que el riesgo hoy es latente — pero la
  asimetría con `ToolsInstaller` (que sí usa `request()` + `allowed_redirect_hosts` para
  exactamente este caso) no tiene motivo.

**Refactor:**

```python
# reddit_client._ensure_session — heredar la protección del gateway
self._session = aiohttp.ClientSession(
    connector=GatewayTCPConnector(self._gateway, limit=10),
    headers={"User-Agent": self._config.user_agent},
    timeout=aiohttp.ClientTimeout(total=self._config.timeout_seconds),
)

# nexus_downloader — usar request() (re-auth de redirects + no reenvía apikey a otro host)
async with await self._gateway.request(
    "GET", files_url, session, headers=headers, timeout=meta_timeout
) as resp:
    ...
```

---

### F6 — MEDIO · CONFIRMADO · **experimental**: `UIBroadcastServer` no invalida sockets vivos al rotar el token, y `broadcast()` muta el set durante la iteración

**Archivo:** `sky_claw/antigravity/comms/ws_daemon.py:360-369` y `:380-397`.
**Escenario:** (c) credenciales rotadas en vuelo + robustez concurrente.

**6a — rotación sin callback de cierre.** `start()` genera el token y arranca la rotación pero
**no registra ningún `register_rotation_callback`**:

```python
# ws_daemon.py:360-369
async def start(self) -> None:
    self._auth.generate()
    await self._auth.start_rotation()   # ← sin register_rotation_callback(...)
    self._server = await websockets.serve(self._handler, self.host, self.port)
```

El token solo se valida en el handshake (`_handler`, `:407-408`). Tras una rotación, los
clientes ya conectados **sobreviven con el token viejo** — exactamente el bug que `WebApp` sí
corrige registrando `close_all_ws_ui_clients` (`app.py:152-153`, anclado por
`tests/test_ws_token_rotation.py` contra la regresión #219). `UIBroadcastServer` es el camino
experimental (solo tests), pero repite un fallo ya resuelto en el camino vivo.

**6b — `broadcast()` itera `self._clients` con `await` interno sin lock:**

```python
# ws_daemon.py:388-397
for ws in self._clients:          # ← itera el set vivo
    try:
        await ws.send(payload)    # ← punto de suspensión
    ...
self._clients -= disconnected
```

En cada `await ws.send`, otra task puede ejecutar `_handler` y hacer `self._clients.add(...)`
(`:414`) o `discard(...)` (`:463`). Mutar el set durante la iteración lanza
`RuntimeError: Set changed size during iteration`, tumbando el broadcast. No hay lock sobre
`_clients` (a diferencia de `WebApp`, que usa `_ws_ui_lock`).

**Refactor:**

```python
# 6a — paridad con WebApp
await self._auth.start_rotation()
self._auth.register_rotation_callback(self._close_all_clients)

# 6b — iterar sobre un snapshot inmutable
for ws in list(self._clients):
    try:
        await ws.send(payload)
    except (ConnectionClosed, ConnectionClosedError):
        disconnected.add(ws)
self._clients -= disconnected
```

---

### F7 — PLAUSIBLE · **experimental**: el token WS tiene tres fuentes sin sincronizador, y dos contratos de auth incompatibles

**Archivos:** `comms/_transport.py:208-222`, `comms/frontend_bridge.py:304-315`,
`comms/interface.py:23,40`, `comms/ws_daemon.py:84`, y los servers Node
`telegram_gateway_node/server.js:168-197`, `telegram_gateway.js:92-127`.
**Escenario:** (c) credenciales que expiran/desalinean en vuelo.

Dos observaciones sobre el camino WS **experimental** (no cableado en prod hoy):

1. **Tres fuentes para "el mismo" token, sin sincronizador visible:** el env estático del Node
   (`WS_AUTH_TOKEN`), el keyring que lee `FrontendBridge._authenticate` (`frontend_bridge.py:304-315`)
   y el **archivo rotativo** que produce `AuthTokenManager` y lee `read_token_file`. El
   `AuthTokenManager` rota el archivo cada 1800s; el env del Node no se entera. Tras la primera
   rotación, los handshakes Python↔Node podrían dejar de coincidir.
2. **Contratos incompatibles:** los clientes Python son **header-based** (`X-Auth-Token` vía
   `authenticated_connect`): `interface.py:40` conecta a `ws://127.0.0.1:18789` (puerto agente
   de `server.js`) y `ws_daemon.py:84` al gateway Node. Pero los servers Node esperan un
   **handshake por mensaje** `{"type":"auth","token"}` (`server.js:168-197`, cierre 4001 a los
   3s si no llega). Los emisores header-based encajan con los servidores **Python**
   (`UIBroadcastServer`, `WebApp`), no con los Node.

**Por qué PLAUSIBLE y no CONFIRMADO:** estos componentes están cableados solo en tests, así que
el pairing real depende de cómo se despliegue el gateway Node (que hoy no participa del camino
vivo). El mecanismo es sólido; falta el despliegue real para cerrarlo.

**Refactor (fuente única del token):**

```javascript
// Node: leer el archivo rotativo del AuthTokenManager en cada handshake, no un env estático
const { token } = JSON.parse(fs.readFileSync(WS_TOKEN_PATH, "utf-8"));
// y unificar el contrato: aceptar X-Auth-Token en el upgrade HTTP (como los servers Python)
```

---

## 🧾 3. Hallazgos menores / notas de hardening

Verificados, de bajo impacto o latentes. No son bugs vivos; se listan para cerrar el modelo
de amenazas.

- **Leak-on-redirect latente en `gateway.request()`** (`network_gateway.py:317`). El bucle de
  redirects copia `hop_kwargs` (incluidos `headers`) al host de `Location`. Hoy **ningún caller
  filtra un secreto** por esta vía: la única ruta que redirige (descarga de GitHub,
  `tools_installer.py:919`) lleva solo `Accept: application/octet-stream`. Como hardening,
  `request()` debería **eliminar headers sensibles** (`Authorization`, `Cookie`, `apikey`)
  cuando un salto cambia de host, para que un futuro caller no filtre por accidente.
- **`_dns_pins` compartido sin lock** (`network_gateway.py:204` + `SafeResolver.resolve`). Dos
  resoluciones concurrentes del mismo `(host,port,family)` pueden ambas hacer miss y resolver
  dos veces (last-write-wins). **No es fallo de seguridad** (ambas validan y pinean); es una
  doble-resolución no serializada. Un `asyncio.Lock` por clave lo elimina si importa el costo.
- **`SSRFValidator._default_resolver` usa `socket.getaddrinfo` síncrono** (`ssrf.py:110`). Hoy
  `validate()` es síncrono y se usa como field-validator pydantic (defensa en profundidad,
  fuera de un `await`), así que no bloquea. Riesgo latente si alguien lo invoca en un path
  async — usar `loop.getaddrinfo` como hace el gateway.

---

## Anexo A — Matriz de hallazgos

| ID | Gravedad | Veredicto | Estado en prod | Archivo:línea | Escenario | Mecanismo |
|----|----------|-----------|----------------|---------------|-----------|-----------|
| F1 | ALTO | CONFIRMADO | **Vivo** | `app_context.py:715-726`, `router.py:373-379` | (c) | Hot-swap Zero-Trust de credenciales inerte: router sin `vault=` → `reload_provider` siempre `False` |
| F2 | ALTO | CONFIRMADO | Experimental | `ws_daemon.py:25-30` | (b) | `import ast_guardian` desde ruta inexistente → guardrail AST inerte + `sys.path` dinámico como superficie de inyección |
| F3 | MEDIO | CONFIRMADO | **Vivo** | `auth_token_manager.py:133-148`, `credential_vault.py:484,503` | (a) | `generate()` (file I/O + `icacls`) y Fernet síncronos dentro del event loop |
| F4 | MEDIO | CONFIRMADO | Durmiente (webhook no montado) | `telegram.py:317,355` | (a)(b) | `session.post` directo: bypass del gateway + fuga de `ClientResponse` + sin timeout |
| F5 | MEDIO | CONFIRMADO | 5a durmiente / 5b vivo | `reddit_client.py:193-200`, `nexus_downloader.py:208-271` | (b) | Sesión sin `SafeResolver` (rebinding) / `session.get()` sin re-auth de redirect y con `apikey` reenviable |
| F6 | MEDIO | CONFIRMADO | Experimental | `ws_daemon.py:360-369,388-397` | (c) | Rotación sin invalidar sockets vivos + `broadcast()` muta el set durante la iteración |
| F7 | PLAUSIBLE | — | Experimental | `_transport.py`, `interface.py:40`, `server.js:168` | (c) | Token WS con 3 fuentes sin sincronizador + contratos header-vs-message incompatibles |
| n1 | MENOR | latente | Vivo | `network_gateway.py:317` | (b) | Leak-on-redirect: headers copiados al host de `Location` (hoy sin secreto en juego) |
| n2 | MENOR | nota | Vivo | `network_gateway.py:204` | — | `_dns_pins` sin lock: doble resolución (no es fallo de seguridad) |
| n3 | MENOR | latente | Vivo | `ssrf.py:110` | (a) | `socket.getaddrinfo` síncrono (hoy fuera de path async) |

## Anexo B — Qué está cableado en producción vs. experimental

| Componente | Estado | Evidencia |
|------------|--------|-----------|
| `NetworkGateway` + sesión compartida | **Vivo** (centro del egress) | `app_context.py:99-130`, reusado por scrapers/providers/tools/telegram |
| Telegram **long-polling** | **Vivo** | `app_context.py:730-747` (`TelegramWebhook.process_update` + `TelegramPolling`) |
| Telegram **webhook** (`handle_update`) | Durmiente | Sin `add_post` que lo monte (único `add_post` = `/api/chat`, `app.py:149`) |
| `_handle_callback_query` (HITL inline) | Durmiente | Solo alcanzable vía webhook; polling descarta callbacks |
| `WebApp` (`/api/chat`, `/ws/ui`) | **Vivo** | `gui/_bootloader.py:358`; rotación de token correcta (`app.py:152-153`) |
| `AuthTokenManager` (métricas + WebApp) | **Vivo** | `app_context.py:591-593`; loop de rotación con `generate()` bloqueante (F3) |
| `CredentialVault` | Solo tests | No cableado al router (F1); `tests/test_credential_vault*.py` |
| `TelegramDaemon` / `UIBroadcastServer` (`ws_daemon.py`) | Solo tests | Import roto (F2); `tests/test_ws_daemon_dispatch.py` |
| `FrontendBridge` | Solo tests | `tests/test_frontend_bridge_transport.py` |
| `RedditKnowledgeResolver` | Solo tests | No instanciado en prod (F5a) |
| Gateway Node (`server.js`, `telegram_gateway.js`) | Experimental | Camino alternativo; contratos WS a verificar (F7) |

---

## Nota metodológica

- **Evidencia vs. sospecha:** cada F cita archivo:línea reales, verificados por lectura
  directa del código en `e43f617`, y contrastados con su test ancla. Dos sospechas iniciales
  se **degradaron tras intentar refutarlas**: el "bypass de gateway en Telegram" resultó
  durmiente (webhook no montado, polling descarta callbacks → F4 MEDIO, no crítico), y el
  "leak-on-redirect" resultó latente (ningún caller filtra un secreto por esa vía hoy → n1
  MENOR). Se reportan igual, con su alcance real, en lugar de inflarlos.
- **Entregable inicial = documento; remediación posterior acotada.** Los fragmentos de
  refactor de §2 son propuestas. En un follow-up sobre esta misma rama se aplicó el
  subconjunto vivo/bajo-riesgo (F3, n1, F4) con TDD y gates verdes — ver "Remediación
  aplicada" arriba. El resto queda diferido con motivo.
- **Sin cierre de T-XX del backlog.** Es una auditoría nueva; no modifica
  `docs/pending_ooda_status.md` (aunque F1 coincide con su residual §2.3 de consolidación de
  secretos).

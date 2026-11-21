# Revisión adversarial — PR #977 `fix(comms): conservar el agente activo al reemplazar su WebSocket`

> **Estado:** evidencia histórica fechada; vale contra el commit que declara. No es fuente viva.

**Revisor:** rol code reviewer + revisor adversarial (Arena Agent), con verificación independiente posterior (§10).
**Fecha:** 2026-11-21
**Base:** `main` @ `dae85a2` · **Head:** `e1993f6` (`fix/591-node-agent-reconnect`) · 1 commit, 3 archivos, +131 / −4
**Issue de origen:** #591 · **PR-C** («gateway Node + `ws_daemon` compatibles con cierre/reconexión real»)

---

## 1. Veredicto

| Campo | Valor |
| --- | --- |
| **Semáforo** | 🟢 **VERDE** — ningún 🔴 ni 🟡 sostenido tras la verificación (§10) |
| **Estado propuesto** | `DONE` |
| **¿Cumple su objetivo?** | **Sí, demostrado** (§3) |
| **¿Genera regresión?** | **No**, en las superficies que el diff toca (§4) |
| **¿Se puede mergear?** | **Sí**; pendiente solo que terminen los checks de Windows (`mergeStateStatus: BLOCKED` por checks `pending`) |

**Resumen:** el fix es correcto, mínimo y bien dirigido —el `close` del socket reemplazado ya no borra la referencia al agente nuevo—, y el test nuevo es rojo en `main` y verde en el PR de forma reproducible. Los puntos que quedan son de precisión del test e higiene del harness (P3, opcionales). `server.js` no necesita cambios.

> La versión inicial de este informe marcaba 🟡 con seis P2. La verificación independiente (§10) refutó dos (P2-1 y P2-6) y rebajó tres a P3; el semáforo se corrigió en consecuencia.

---

## 2. El cambio, línea por línea

```js
// server.js:258-267  (antes: cerrar → asignar ; ahora: asignar → cerrar)
const agenteAnterior = agentSocket;
agentSocket = ws;
if (agenteAnterior && agenteAnterior !== ws && agenteAnterior.readyState === ws.OPEN) {
    try { agenteAnterior.close(4000, 'Replaced by new agent connection'); }
    catch (closeErr) { console.warn('[AGENT] Error cerrando agente previo:', closeErr.message); }
}

// server.js:303-304
ws.on('close', () => {
    if (agentSocket !== ws) return;   // evita que el close tardío de A anule a B
    ...
});
```

**Diagnóstico del bug original (confirmado):** al entrar B se cerraba A *antes* de `agentSocket = B`. El `close` de A se emite de forma asíncrona, así que su handler corría después de `agentSocket = B` y hacía `agentSocket = null` + `[BUFFERING]` + `_scheduleReconnectNotify()`, con B conectado. Efectos: `/health` reportaba `agent_connected:false`, los comandos de la UI se encolaban en `pendingCommands` con un agente sano del otro lado, y el canal entraba en escalada de backoff.

**Nota de precisión:** no es una race TOCTOU; es una condición de orden lógico (el cierre de un par se imputaba al reemplazo). El fix es determinista, no una mitigación probabilística.

---

## 3. Verificación de que cumple el objetivo

| Prueba | Resultado | Reproducido en §10 |
| --- | --- | --- |
| Test nuevo sobre `server.js` de **main** | **exit 1**, falla en `agent_connected === true` (`test_agent_reconnect.js:104`, `expected: true, actual: false`) | **Sí** |
| Test nuevo en el **head del PR** | **exit 0** | **Sí** (Node 22.23.5) |
| Estabilidad | 25 + 18 corridas verdes, 0 flakes (Node 22) | Parcial: 6 corridas verdes, 0 flakes |
| Node 24 (versión de CI) | verde | No |
| Tests vecinos `test_timing_safe_equal.js`, `test_openssl_no_shell_injection.js` | pass (son scripts con `process.exit`, no `node:test`) | **Sí** (exit 0 ambos) |
| Anclas Python que leen `ci.yml` (`test_ci_platform_policy`, `test_completion_policy_invariant`, `test_release_smoke_wiring`, `test_qodo_workflow_invariant`, `test_registrar_sha256_ejecutable`, `test_native_parallax_producto_invariant`) | 235 passed según el revisor | **Sí, con discrepancia:** 251 passed (`--noconftest`, `-o addopts=""`) |

El criterio de aceptación del issue («A → B → cerrar A → B sigue activo y recibiendo comandos») está cubierto y se extiende: valida el flujo UI→agente, la retransmisión agente→UI y que el `[BUFFERING]` **sí** aparezca cuando el agente realmente se va (líneas 118-121), lo que evita «arreglar» el bug silenciando el aviso.

---

## 4. Análisis de regresión

1. **`agentSocket` (asignaciones en 200, 260, 306).** El `close` de un socket que no es el activo ya no limpia estado. No hay secuencia en la que `agentSocket` quede apuntando a un socket muerto *siendo* el activo, que es el único caso que rompe `/health` y el despacho.
2. **Handler `message` del agente (277-298).** Sin cambios. Un socket reemplazado que emita un mensaje residual sigue retransmitiéndose a las UIs; es preexistente.
3. **`pendingCommands`.** Se drena una sola vez, al conectar. Sin cambios.
4. **`_cancelReconnectTimer()`.** Sigue llamándose al autenticar. Sin cambios.
5. **Lado UI (auth, rate limit, mutex).** Sin tocar.
6. **CI.** Agrega un step dentro del job `security` (`runs-on: windows-latest`) antes de `npm audit`; no cambia `needs`, nombres de checks ni anclas AST.

**Riesgo diferencial del CI:** `security` está en `build.needs`, así que un fallo de este test bloquea el build del `.exe`. Es lo deseado, pero convierte un test local de sockets en gate de release. Puertos efímeros (`port: 0`) en `127.0.0.1`; el riesgo de flake en Windows es bajo pero **no está medido en Windows**.

**Observación de valor:** los otros dos tests Node del directorio **nunca se ejecutan en CI** (verificado: `ci.yml` no los referencia) y no usan `node:test`. Este PR crea el primer test Node que corre en el pipeline. Unificarlos es alcance aparte.

---

## 5. Batería de mutación (¿el test fija el fix?)

Ocho mutaciones sobre el `server.js` del PR; **las ocho se reprodujeron en §10** con el mismo resultado.

| # | Mutación | ¿Detectada? |
| --- | --- | --- |
| M1 | quitar `if (agentSocket !== ws) return;` | **Sí** |
| M2 | volver al orden original (cerrar → asignar), conservando la guarda | **No** |
| M3 | revertir ambos (código de `main`) | **Sí** |
| M4 | no cerrar el agente anterior | **Sí** |
| M5 | quitar `_cancelReconnectTimer()` | **No** |
| M6 | no drenar `pendingCommands` al reconectar | **No** |
| M7 | no emitir `[READY] Daemon connected` a las UIs | **No** |
| M8 | hacer que el `close` limpie siempre | **Sí** |

**Lectura:** el test es de **identidad del socket** y la guarda `agentSocket !== ws` lo sostiene. M2 es *equivalente a nivel de comportamiento observable* con la guarda presente, así que no es un defecto del PR. M5–M7 mutan comportamiento que el diff **no toca**; que no se detecten no es una brecha de este cambio (ver P3-1).

---

## 6. Hallazgos

### 🔴 ROJO — ninguno

No hay P0/P1: sin fuga de datos, sin falso verde (exit codes verificados), sin hermano olvidado del mismo fix (ver P3-3 y §7), sin gate roto.

### 🟡 AMARILLO — ninguno

Los P2 de la versión inicial se refutaron o rebajaron; ver §10.

### 🟢 VERDE — P3 (opcionales; no commitear por esto)

- **P3-1 · Anclas parciales del propio cambio (antes P2-3).** Ningún test protege `const agenteAnterior = agentSocket` ni el orden asignar→cerrar (M2), porque con la guarda es observablemente equivalente. M5/M6/M7 son comportamiento no modificado por el PR. Mejora opcional y barata: un caso `A → B → cae B` que verifique `agent_connected === false` y un único `[BUFFERING]`; el test actual ya tiene esa mitad en las líneas 118-121.
- **P3-2 · `agente.clients.values().next().value` (test:93, antes P2-2).** Asume un solo cliente. Por construcción lo es en este punto del test (solo se conectó A); sería frágil solo si alguien agrega otro cliente antes. Opcional: `assert.equal(agente.clients.size, 1)`.
- **P3-3 · Hermano `telegram_gateway.js:105-114` (antes P2-4).** Tiene el mismo orden cerrar→asignar (`daemonSocket.close(4000, …)` en 109, `daemonSocket = ws` en 114), pero su `close` ya está guardado con `if (daemonSocket === ws)` (línea 158) y comenta el porqué. No hay defecto demostrable. Conviene que el cuerpo del PR lo diga.
- **P3-4 · Harness con `process` mínimo (test:54-58, antes P2-5).** Inyecta solo `env`, `on` y `uptime`. `server.js` usa `process.exit` únicamente si falta `WS_AUTH_TOKEN` (línea 130), que el test define; un uso nuevo de `process` en producción fallaría en el harness, no en el producto. Documentar como contrato («los globals inyectados deben seguir a `server.js`»). El riesgo de un segundo `http.createServer()` es hipotético: hoy hay una sola llamada (línea 414).
- **P3-5 · `if (agentSocket && agentSocket.readyState === 1)` en el handler de UI (server.js:381)** usa check-then-send dentro del mutex. Preexistente, el `catch` re-encola. No se toca.
<!-- coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated-section:v1:e03f71850a303fd9700ad20fec70bc6b849fc44176b6f4bd14c8896d3934065c -->

- **P3-6 · `node --test tests/*.js` con glob sin comillas.** En el head del PR, Node 22.23.5 ejecuta los tres archivos y acepta cada uno si termina con código 0, incluso si no usa `node:test`; un código distinto de 0 falla. El step de CI ejecuta únicamente `tests/test_agent_reconnect.js`.

<!-- /coderabbit-ai-generated:v1 -->
- **P3-7 · Sin eslint/prettier** en el gateway: las líneas largas del test nuevo no tienen gate de estilo.
- **P3-8 · `CHANGELOG.md`:** el changelog **sí** menciona el gateway Node (p. ej. reproducibilidad con `npm ci` + `npm audit`, líneas ~1036-1038), pero no hay convención de entrada por fix de comportamiento del gateway; no se exige una.
- **P3-9 · `_notifyDaemonStatus` itera `uiSockets` sin snapshot** con `delete` concurrente en el `close` de una UI. Teórico y preexistente.

---

## 7. Lo que el PR **no** hace (y no debería hacer acá)

El issue #591 define PR-C como «gateway Node **+** `ws_daemon`». Este PR cubre solo el gateway Node. `ws_daemon.py` **ya no consulta `.open`** (0 coincidencias) y sus rutas de progreso/respuesta/error ya manejan `ConnectionClosed`/`ConnectionClosedError` (líneas 95, 401, 431, 508). El hermano está resuelto o nunca llegó a `main`; el PR no deja un hermano olvidado. Conviene que el cuerpo del PR lo declare, porque el issue asigna ambos al mismo punto de entrega.

El criterio del issue «revisar que las notificaciones/reconnect timers sigan el mismo criterio de identidad» queda cubierto por la guarda del `close`: al no ser el socket activo, el handler retorna antes de `_scheduleReconnectNotify()`.

---

## 8. Checklist de gate

| Gate | Estado |
| --- | --- |
| Ruff check / format, Mypy | ✅ sin impacto (0 archivos Python) |
| Pytest (anclas que leen `ci.yml`) | ✅ 251 passed en local (§10) |
| Nuevo test Node | ✅ verde en Node 22; ✅ rojo en `main` (no falso verde) |
| Branch protection / checks | ⚠️ `mergeStateStatus: BLOCKED` por checks `pending` de Windows; no es defecto del PR |
| CodeRabbit | ⚠️ sin revisión (rate limit del proveedor); Codex: «Didn't find any major issues» sobre `e1993f6` |

---

## 9. Recomendación

**Mergear cuando cierren los checks de Windows.** Opcional, sin ampliar alcance y sin reiniciar CI por ello: dejar en el cuerpo del PR (a) que `telegram_gateway.js` ya tiene la guarda (P3-3) y (b) que `ws_daemon.py` no consulta `.open` (§7). Todo lo demás es P3.

---

## 10. Verificación independiente (2026-11-21)

Se contrastó este informe contra el repositorio real: `git fetch` del head `e1993f6`, worktree aislado, `npm ci`, Node 22.23.5. Resultado: la mayoría de las afirmaciones se sostiene; dos hallazgos eran incorrectos.

**Confirmado ejecutando:** diff de `server.js` y `ci.yml`; rojo en `main` y verde en el PR (§3); las ocho mutaciones de §5; los dos tests vecinos; las anclas Python (251 passed); línea 130 `process.exit`, líneas 200/260/306 de `agentSocket`, 381 del handler de UI; `ws_daemon.py` sin `.open`; los otros dos tests Node ausentes de `ci.yml`; runner `windows-latest` del job `security`; rate limit de CodeRabbit.

**Refutado (descartado del informe):**

- **P2-1 (antes «aserción que no puede fallar»).** Se afirmaba que en `main` el test falla en la línea 97 por `AbortError`, porque A «nunca se cierra». Falso: en `main` el servidor sí cierra A con 4000 (`readyState === OPEN`), el test pasa la línea 97 y falla en la **línea 104** con `ERR_ASSERTION`, `expected: true, actual: false`. El propio §3 de la versión inicial ya decía 104; la contradicción interna era la señal.
- **P2-6 (antes «el test podría escribir certificados en el árbol de trabajo»).** Falso: el harness define **ambas** `SSL_KEY_PATH` y `SSL_CERT_PATH`, así que `getOrCreateTlsCerts()` retorna `null` en la rama de variables de entorno (server.js:72) y **nunca llega** al fallback de `HOME`/`openssl` (líneas 93-110). `HOME: os.tmpdir()` no cambia nada.

**Rebajado a P3:** P2-2 → P3-2, P2-3 → P3-1, P2-5 → P3-4 (ver §6).

**Corregido:** P3-4 de la versión inicial decía que no había entrada de changelog sobre el gateway; sí hay (P3-8 actual). Conteo de anclas Python: 251, no 235.

**No reproducido** (se conserva como dato del revisor original): Node 24, las 43 corridas de estabilidad (aquí 6), la ejecución en Windows, el parseo YAML del workflow.

---

### Anexo — comandos reproducibles

```bash
# rojo en main / verde en el PR (desde la raíz del repo, con el head fetcheado)
git worktree add .worktrees/pr977 e1993f6
cd .worktrees/pr977/sky_claw/app/comms/telegram_gateway_node && npm ci
node --test tests/test_agent_reconnect.js ; echo $?   # 0
git show dae85a2:sky_claw/app/comms/telegram_gateway_node/server.js > server.js
node --test tests/test_agent_reconnect.js ; echo $?   # 1 (línea 104)
git checkout -- server.js

# estabilidad
for i in $(seq 1 25); do node --test tests/test_agent_reconnect.js >/dev/null || echo FLAKE; done
```

```bash
# anclas Python que leen ci.yml (desde la raíz del worktree del PR)
python -m pytest --noconftest -o addopts="" -q \
  tests/test_ci_platform_policy.py tests/test_completion_policy_invariant.py \
  tests/test_release_smoke_wiring.py tests/test_qodo_workflow_invariant.py \
  tests/test_registrar_sha256_ejecutable.py tests/test_native_parallax_producto_invariant.py
```

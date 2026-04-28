/**
 * Sky-Claw Gateway (Node.js 24)
 * Middleware de alta disponibilidad entre la interfaz web y el daemon de Python.
 */

const { WebSocketServer } = require('ws');
const { Mutex } = require('async-mutex');
const path = require('path');
const crypto = require('crypto');
const https = require('https');
const fs = require('fs');
const { execSync } = require('child_process');
require('dotenv').config({ path: path.join(__dirname, '.env') });

function getOrCreateTlsCerts() {
    // SEC-006: Priorizar certificados desde variables de entorno
    const sslKeyPath = process.env.SSL_KEY_PATH;
    const sslCertPath = process.env.SSL_CERT_PATH;

    // Si ambas variables de entorno están definidas, intentar cargar certificados
    if (sslKeyPath && sslCertPath) {
        try {
            if (fs.existsSync(sslKeyPath) && fs.existsSync(sslCertPath)) {
                const creds = {
                    key: fs.readFileSync(sslKeyPath),
                    cert: fs.readFileSync(sslCertPath)
                };
                console.log(JSON.stringify({
                    level: "INFO",
                    msg: "SSL certificates loaded successfully from environment paths",
                    key_path: sslKeyPath,
                    cert_path: sslCertPath,
                    ts: Date.now()
                }));
                return creds;
            } else {
                console.error(JSON.stringify({
                    level: "ERROR",
                    msg: "SSL certificate files not found at specified paths",
                    key_path: sslKeyPath,
                    cert_path: sslCertPath,
                    ts: Date.now()
                }));
                // Fallback a modo inseguro
                console.warn(JSON.stringify({
                    level: "WARN",
                    msg: "SSL certs not found. Running in degraded INSECURE mode (ws://)",
                    ts: Date.now()
                }));
                return null;
            }
        } catch (err) {
            console.error(JSON.stringify({
                level: "ERROR",
                msg: "Failed to read SSL certificates",
                error: err.message,
                ts: Date.now()
            }));
            // Degradación elegante: no terminar el proceso
            console.warn(JSON.stringify({
                level: "WARN",
                msg: "SSL certs not found. Running in degraded INSECURE mode (ws://)",
                ts: Date.now()
            }));
            return null;
        }
    }

    // Fallback: Intentar certificados auto-generados (comportamiento anterior)
    const certDir = path.join(process.env.HOME || process.env.USERPROFILE || '.', '.sky_claw', 'certs');
    const keyPath = path.join(certDir, 'server.key');
    const certPath = path.join(certDir, 'server.crt');
    if (fs.existsSync(keyPath) && fs.existsSync(certPath)) {
        return { key: fs.readFileSync(keyPath), cert: fs.readFileSync(certPath) };
    }
    try {
        fs.mkdirSync(certDir, { recursive: true });
        execSync(`openssl req -x509 -newkey rsa:2048 -keyout "${keyPath}" -out "${certPath}" -days 365 -nodes -subj "/CN=localhost"`, { stdio: 'pipe' });
        return { key: fs.readFileSync(keyPath), cert: fs.readFileSync(certPath) };
    } catch (err) {
        console.warn(JSON.stringify({
            level: "WARN",
            msg: "SSL certs not found. Running in degraded INSECURE mode (ws://)",
            detail: "openssl not available",
            error: err.message,
            ts: Date.now()
        }));
        return null;
    }
}

// Configuración de puertos (Zero Trust Local)
const AGENT_PORT = 18789;
const UI_PORT = 18790;
const BIND_ADDRESS = '127.0.0.1';

// SC-V01: Token de autenticación — obligatorio en entorno (Fail-Fast Pattern)
const WS_AUTH_TOKEN = process.env.WS_AUTH_TOKEN;
if (!WS_AUTH_TOKEN) {
    console.error(JSON.stringify({
        level: "FATAL",
        message: "WS_AUTH_TOKEN environment variable missing",
        timestamp: new Date().toISOString(),
        source: "gateway",
        action: "process_exit"
    }));
    process.exit(1);
}

/**
 * Compara dos strings de forma segura contra ataques de timing side-channel.
 * Utiliza crypto.timingSafeEqual para evitar filtración de información por tiempo de respuesta.
 * @param {string} a - Primer string a comparar
 * @param {string} b - Segundo string a comparar
 * @returns {boolean} - true si son iguales, false en caso contrario
 */
function timingSafeEqual(a, b) {
    // Validar que ambos sean strings
    if (typeof a !== 'string' || typeof b !== 'string') {
        return false;
    }
    // Convertir a Buffers antes de comparar longitudes para manejar UTF-8 multibyte
    const bufA = Buffer.from(a, 'utf8');
    const bufB = Buffer.from(b, 'utf8');
    // Si las longitudes no coinciden, retornar false sin lanzar error
    if (bufA.length !== bufB.length) {
        return false;
    }
    // Comparar de forma segura
    try {
        return crypto.timingSafeEqual(bufA, bufB);
    } catch {
        return false;
    }
}

/**
 * Exige un handshake de autenticación antes de procesar mensajes.
 * El cliente debe enviar {"type":"auth","token":"<token>"} como primer mensaje.
 * Si no autentica en 3s → cierre 4001. Token incorrecto → cierre 4003.
 * @param {import('ws').WebSocket} ws
 * @param {string} label  Etiqueta de log ('AGENT' o 'UI')
 * @param {Function} onAuthenticated  Callback ejecutado tras auth exitosa
 */
function requireAuth(ws, label, onAuthenticated) {
    const timer = setTimeout(() => {
        console.warn(`[${label}] Auth timeout — cerrando conexión no autenticada.`);
        ws.close(4001, 'auth_timeout');
    }, 3000);

    ws.once('message', (raw) => {
        try {
            const msg = JSON.parse(raw.toString());
            if (msg.type === 'auth' && timingSafeEqual(msg.token, WS_AUTH_TOKEN)) {
                clearTimeout(timer);
                ws.send(JSON.stringify({ type: 'auth_ok' }));
                onAuthenticated();
            } else {
                clearTimeout(timer);
                console.log(JSON.stringify({
                    event: "auth_failure",
                    timestamp: new Date().toISOString(),
                    source: "gateway",
                    remote: ws._socket?.remoteAddress || "unknown",
                    reason: "invalid_token"
                }));
                ws.close(4003, 'unauthorized');
            }
        } catch {
            clearTimeout(timer);
            ws.close(4003, 'unauthorized');
        }
    });
}

// Estado del Gateway
let agentSocket = null;
// CONCURRENCY: Mutex prevents a TOCTOU race where agentSocket is checked
// as OPEN but closes before send() executes across concurrent UI messages.
const agentMutex = new Mutex();
const uiSockets = new Set();
const pendingCommands = [];

// F2.3: Exponential-backoff reconnect notifications to UI clients.
// The gateway is a server (it doesn't dial out to the daemon), so there is
// nothing to retry here. Instead we broadcast escalating STATUS messages to
// every connected UI so operators know the daemon is still gone, without
// spamming them at a fixed 1-second rate.
const RECONNECT_BASE_MS = 1000;
const RECONNECT_MAX_MS = 30000;
const RECONNECT_MAX_ATTEMPTS = 10;
let _reconnectAttempts = 0;
let _reconnectTimer = null;

function _notifyDaemonStatus(content) {
    uiSockets.forEach(ui => {
        if (ui.readyState === 1) ui.send(JSON.stringify({ type: 'STATUS', content }));
    });
}

function _scheduleReconnectNotify() {
    if (_reconnectAttempts >= RECONNECT_MAX_ATTEMPTS) {
        _notifyDaemonStatus('[OFFLINE] Daemon unreachable — manual intervention required');
        return;
    }
    const delay = Math.min(RECONNECT_BASE_MS * Math.pow(2, _reconnectAttempts), RECONNECT_MAX_MS);
    _reconnectAttempts++;
    _reconnectTimer = setTimeout(() => {
        if (!agentSocket) {
            _notifyDaemonStatus(`[BUFFERING] Daemon offline — awaiting reconnect (attempt ${_reconnectAttempts}/${RECONNECT_MAX_ATTEMPTS})`);
            _scheduleReconnectNotify();
        }
    }, delay);
}

function _cancelReconnectTimer() {
    if (_reconnectTimer !== null) {
        clearTimeout(_reconnectTimer);
        _reconnectTimer = null;
    }
    _reconnectAttempts = 0;
}

const tlsCreds = getOrCreateTlsCerts();

// --- Servidor para el Agente Python (Daemon) ---
const agentServer = new WebSocketServer({ port: AGENT_PORT, host: BIND_ADDRESS, maxPayload: 1 * 1024 * 1024 });

agentServer.on('connection', (ws) => {
    requireAuth(ws, 'AGENT', () => {
        console.log(`[AGENT] Daemon autenticado desde ${ws._socket.remoteAddress}`);
        // F2.3: Daemon reconnected — cancel any pending backoff notification timer.
        _cancelReconnectTimer();

        // Cerrar agente previo antes de asignar el nuevo (previene socket huérfano)
        if (agentSocket && agentSocket !== ws && agentSocket.readyState === ws.OPEN) {
            try {
                agentSocket.close(4000, 'Replaced by new agent connection');
            } catch (closeErr) {
                console.warn('[AGENT] Error cerrando agente previo:', closeErr.message);
            }
        }
        agentSocket = ws;

        // Procesar cola de comandos pendientes (Resiliencia de Estado)
        while (pendingCommands.length > 0 && agentSocket.readyState === ws.OPEN) {
            const cmd = pendingCommands.shift();
            console.log(`[AGENT] Despachando comando encolado: ${cmd.type}`);
            agentSocket.send(JSON.stringify(cmd));
        }

        ws.on('message', (data) => {
            const response = data.toString();
            try {
                const parsed = JSON.parse(response);
                // Telemetría: Retransmitir silenciosamente a la UI
                if (parsed.type === 'TELEMETRY') {
                    uiSockets.forEach(ui => {
                        if (ui.readyState === 1) ui.send(response);
                    });
                    return;
                }
            } catch (e) {
                // No es JSON, tratar como mensaje normal
            }

            // Retransmitir respuestas normales del agente al frontend
            console.log(`[AGENT] Mensaje recibido: ${response.substring(0, 50)}...`);
            uiSockets.forEach(ui => {
                if (ui.readyState === 1) ui.send(response);
            });
        });

        // Notificar a las UIs que el agente está listo
        uiSockets.forEach(ui => {
            if (ui.readyState === 1) ui.send(JSON.stringify({ type: 'STATUS', content: '[READY] Daemon connected' }));
        });

        ws.on('close', () => {
            console.warn('[AGENT] Daemon desconectado. Entrando en modo de espera/buffer.');
            agentSocket = null;
            // F2.3: Broadcast immediate disconnect notice then start exponential-backoff
            // status updates so UIs remain informed without being flooded.
            _notifyDaemonStatus('[BUFFERING] Daemon desconectado — Reconnecting...');
            _scheduleReconnectNotify();
        });

        ws.on('error', (err) => {
            console.error(`[AGENT] Error en socket del daemon: ${err.message}`);
        });
    }); // end requireAuth
});

console.log(`[GATEWAY] Escuchando Agente Python en ws://${BIND_ADDRESS}:${AGENT_PORT}`);

// --- Servidor para la Interfaz Web (Frontend) ---
let uiHttpsServer = null;
let uiServer;
if (tlsCreds) {
    uiHttpsServer = https.createServer(tlsCreds);
    uiServer = new WebSocketServer({ server: uiHttpsServer, maxPayload: 1 * 1024 * 1024 });
    uiHttpsServer.listen(UI_PORT, BIND_ADDRESS, () => console.log(`[GW] UI WS (wss://): port ${UI_PORT}`));
} else {
    uiServer = new WebSocketServer({ port: UI_PORT, host: BIND_ADDRESS, maxPayload: 1 * 1024 * 1024 });
    console.log(`[GW] UI WS (ws://): port ${UI_PORT}`);
}

uiServer.on('connection', (ws) => {
    requireAuth(ws, 'UI', () => {
        console.log(`[UI] Nueva conexión de interfaz web autenticada.`);
        uiSockets.add(ws);

        // C-06: Rate limiting con ventana deslizante real (10 mensajes / 1 s).
        // La versión anterior usaba ventana fija (reset cada 1 s) que permitía
        // ráfagas de hasta 20 mensajes en el límite entre ventanas. Aquí
        // mantenemos los timestamps de los últimos mensajes y filtramos por
        // antigüedad antes de cada decisión: imposible exceder N en cualquier
        // ventana de tamaño W. Tope de memoria por socket: RATE_LIMIT_MAX entradas.
        const RATE_LIMIT_WINDOW_MS = 1000;
        const RATE_LIMIT_MAX = 10;
        ws._messageTimestamps = [];

        ws.on('message', (data) => {
            const now = Date.now();
            // Descartar timestamps fuera de la ventana en O(k) donde k <= MAX.
            ws._messageTimestamps = ws._messageTimestamps.filter(
                ts => now - ts < RATE_LIMIT_WINDOW_MS
            );

            if (ws._messageTimestamps.length >= RATE_LIMIT_MAX) {
                // Drop silencioso — el límite NO se desbloquea hasta que
                // el timestamp más antiguo salga de la ventana.
                console.log(JSON.stringify({
                    event: "rate_limit",
                    timestamp: new Date().toISOString(),
                    source: "gateway",
                    remote: ws._socket?.remoteAddress || "unknown"
                }));
                return;
            }
            ws._messageTimestamps.push(now);

            let command;
            try {
                command = JSON.parse(data);
            } catch (err) {
                console.error('[UI] Error al procesar mensaje de UI:', err.message);
                return;
            }
            console.log(`[UI] Comando recibido: ${command.type || 'unknown'}`);

            // CONCURRENCY: Hold the mutex for the check-then-send to eliminate
            // the TOCTOU window. runExclusive returns a Promise so the handler
            // is async-safe with the existing async WS callback.
            agentMutex.runExclusive(() => {
                if (agentSocket && agentSocket.readyState === 1) {
                    try {
                        // Enviar inmediatamente si el agente está vivo
                        agentSocket.send(data.toString());
                    } catch (sendErr) {
                        // Socket closed between check and send — re-enqueue.
                        console.warn('[GATEWAY] Send falló, re-encolando:', sendErr.message);
                        pendingCommands.push(command);
                        if (pendingCommands.length > 100) pendingCommands.shift();
                    }
                } else {
                    // Buffer de Resiliencia: Encolar si el agente está reiniciando
                    console.warn('[GATEWAY] Agente offline. Encolando comando.');
                    pendingCommands.push(command);

                    // Limitar tamaño del buffer para evitar fugas de memoria
                    if (pendingCommands.length > 100) pendingCommands.shift();
                }
            }).catch(err => console.error('[GATEWAY] Mutex error:', err.message));
        });

        ws.on('close', () => {
            console.log('[UI] Interfaz web desconectada.');
            // C-06: Liberar el array de timestamps para que el GC reclame memoria.
            ws._messageTimestamps = null;
            uiSockets.delete(ws);
        });
    }); // end requireAuth
});

// --- Health Check HTTP Endpoint ---
const http = require('http');
const HEALTH_PORT = parseInt(process.env.HEALTH_PORT, 10) || 18791;
const healthServer = http.createServer((req, res) => {
    if (req.url === '/health' && req.method === 'GET') {
        const status = {
            status: 'ok',
            timestamp: new Date().toISOString(),
            agent_connected: agentSocket !== null,
            ui_connections: uiSockets.size,
            uptime_seconds: Math.floor(process.uptime()),
        };
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify(status));
    } else {
        res.writeHead(404);
        res.end();
    }
});
healthServer.listen(HEALTH_PORT, BIND_ADDRESS, () => {
    console.log(`[GW] Health: http://${BIND_ADDRESS}:${HEALTH_PORT}/health`);
});

// Manejo de errores globales para el proceso Node
process.on('uncaughtException', (err) => {
    console.error('[CRITICAL] Error no capturado en el Gateway:', err);
});

// H-11: Captura global de promesas rechazadas sin catch
process.on('unhandledRejection', (reason, promise) => {
    console.error('[CRITICAL] Unhandled Rejection at:', promise, 'reason:', reason);
});

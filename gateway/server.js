/**
 * Sky-Claw Gateway (Node.js 24)
 * Middleware de alta disponibilidad entre la interfaz web y el daemon de Python.
 */

const { WebSocketServer } = require('ws');

// Configuración de puertos (Zero Trust Local)
const AGENT_PORT = 18789;
const UI_PORT = 18790;
const BIND_ADDRESS = '127.0.0.1';

// Estado del Gateway
let agentSocket = null;
const uiSockets = new Set();
const pendingCommands = [];

// --- Servidor para el Agente Python (Daemon) ---
const agentServer = new WebSocketServer({ port: AGENT_PORT, host: BIND_ADDRESS });

agentServer.on('connection', (ws) => {
    console.log(`[AGENT] Daemon conectado desde ${ws._socket.remoteAddress}`);
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
        // Notificar a las UIs sobre la caída (Chaos resilience)
        uiSockets.forEach(ui => {
            if (ui.readyState === 1) ui.send(JSON.stringify({ type: 'STATUS', content: '[BUFFERING] Reconnecting to Daemon....' }));
        });
    });

    ws.on('error', (err) => {
        console.error(`[AGENT] Error en socket del daemon: ${err.message}`);
    });
});

console.log(`[GATEWAY] Escuchando Agente Python en ws://${BIND_ADDRESS}:${AGENT_PORT}`);

// --- Servidor para la Interfaz Web (Frontend) ---
const uiServer = new WebSocketServer({ port: UI_PORT, host: BIND_ADDRESS });

uiServer.on('connection', (ws) => {
    console.log(`[UI] Nueva conexión de interfaz web.`);
    uiSockets.add(ws);

    ws.on('message', (data) => {
        try {
            const command = JSON.parse(data);
            console.log(`[UI] Comando recibido: ${command.type || 'unknown'}`);

            if (agentSocket && agentSocket.readyState === 1) {
                // Enviar inmediatamente si el agente está vivo
                agentSocket.send(data.toString());
            } else {
                // Buffer de Resiliencia: Encolar si el agente está reiniciando
                console.warn('[GATEWAY] Agente offline. Encolando comando.');
                pendingCommands.push(command);
                
                // Limitar tamaño del buffer para evitar fugas de memoria
                if (pendingCommands.length > 100) pendingCommands.shift();
            }
        } catch (err) {
            console.error('[UI] Error al procesar mensaje de UI:', err.message);
        }
    });

    ws.on('close', () => {
        console.log('[UI] Interfaz web desconectada.');
        uiSockets.delete(ws);
    });
});

console.log(`[GATEWAY] Escuchando Interfaz Web en ws://${BIND_ADDRESS}:${UI_PORT}`);

// Manejo de errores globales para el proceso Node
process.on('uncaughtException', (err) => {
    console.error('[CRITICAL] Error no capturado en el Gateway:', err);
});

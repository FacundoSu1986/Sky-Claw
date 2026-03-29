/* frontend/js/app.js */

const UI_PORT = 18790;
const GATEWAY_URL = `ws://127.0.0.1:${UI_PORT}`;

// Referencias del DOM
const chatLog = document.getElementById('chat-log');
const commandInput = document.getElementById('command-input');
const sendBtn = document.getElementById('send-btn');
const overlay = document.getElementById('arcane-overlay');
const statusBadge = document.getElementById('status-badge');
const statusText = document.getElementById('status-text');
const cpuVal = document.getElementById('cpu-val');
const ramVal = document.getElementById('ram-val');
const telemetryHud = document.getElementById('telemetry-hud');

let socket = null;

/**
 * Inicializa la conexión con el Gateway
 */
function initConnection() {
    console.log(`[UI] Intentando conectar con el Gateway en ${GATEWAY_URL}...`);
    
    socket = new WebSocket(GATEWAY_URL);

    socket.onopen = () => {
        console.log('[UI] Conexión establecida con el Gateway.');
    };

    socket.onmessage = (event) => {
        try {
            const data = JSON.parse(event.data);
            handleMessage(data);
        } catch (err) {
            console.error('[UI] Error al procesar mensaje:', err.message);
            // Si no es JSON, renderizar como texto plano de emergencia
            renderMessage('agent', event.data);
        }
    };

    socket.onclose = () => {
        console.warn('[UI] Conexión cerrada. Reintentando en 3 segundos...');
        setBufferingState(true);
        setTimeout(initConnection, 3000);
    };

    socket.onerror = (err) => {
        console.error('[UI] Fallo en el socket:', err.message);
    };
}

/**
 * Procesa los mensajes recibidos del Gateway
 */
function handleMessage(data) {
    // Si el mensaje es un STATUS (Control del Gateway)
    if (data.type === 'STATUS') {
        const content = data.content;
        
        if (content.includes('[BUFFERING]')) {
            setBufferingState(true);
        } else if (content.includes('[READY]')) {
            setBufferingState(false);
        }
        return;
    }

    // Telemetría (Fase 4.1)
    if (data.type === 'TELEMETRY') {
        const stats = data.content;
        cpuVal.innerText = stats.cpu;
        ramVal.innerText = stats.ram;
        return;
    }

    // Si el mensaje es una respuesta del Agente (Daemon Python)
    if (data.type === 'RESPONSE' || data.type === 'QUERY' || data.content) {
        renderMessage('agent', data.content || data.message || JSON.stringify(data));
    }
}

/**
 * Gestiona el estado visual ante fallos del Daemon (Buffering)
 */
function setBufferingState(isBuffering) {
    if (isBuffering) {
        overlay.classList.remove('hidden');
        commandInput.disabled = true;
        sendBtn.disabled = true;
        
        statusBadge.className = 'status-badge buffering';
        statusText.innerText = 'ENLAZANDO...';
        
        telemetryHud.classList.add('disconnected');
        cpuVal.innerText = '--';
        ramVal.innerText = '--';
    } else {
        overlay.classList.add('hidden');
        commandInput.disabled = false;
        sendBtn.disabled = false;
        
        statusBadge.className = 'status-badge ready';
        statusText.innerText = 'SISTEMA LISTO';
        telemetryHud.classList.remove('disconnected');
        
        // Foco en el input automáticamente al recuperar conexión
        commandInput.focus();
    }
}

/**
 * Renderiza un mensaje en el área de log
 */
function renderMessage(sender, content) {
    const messageDiv = document.createElement('div');
    messageDiv.className = `message ${sender}`;
    
    // Si es del agente, usar Marked.js para el Markdown
    if (sender === 'agent' && window.marked) {
        messageDiv.innerHTML = marked.parse(content);
    } else {
        messageDiv.innerText = content;
    }

    chatLog.appendChild(messageDiv);
    
    // Auto-scroll al final
    chatLog.scrollTo({
        top: chatLog.scrollHeight,
        behavior: 'smooth'
    });
}

/**
 * Envía un comando al Gateway
 */
function sendCommand() {
    const text = commandInput.value.trim();
    if (!text || commandInput.disabled) return;

    const payload = {
        type: 'QUERY',
        content: text,
        timestamp: new Date().toISOString(),
        id: crypto.randomUUID ? crypto.randomUUID() : Math.random().toString(36).substring(7)
    };

    // Renderizar mi propia pregunta localmente
    renderMessage('user', text);
    
    // Enviar a través del socket
    socket.send(JSON.stringify(payload));
    
    // Limpiar input
    commandInput.value = '';
}

// Event Listeners
sendBtn.addEventListener('click', sendCommand);
commandInput.addEventListener('keypress', (e) => {
    if (e.key === 'Enter') sendCommand();
});

// Inicio del ciclo de vida
document.addEventListener('DOMContentLoaded', () => {
    initConnection();
    
    // Configuración básica de Marked para bloques de código
    if (window.marked) {
        marked.setOptions({
            breaks: true,
            gfm: true
        });
    }
});

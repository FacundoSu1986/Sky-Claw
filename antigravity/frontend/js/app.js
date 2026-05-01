/* frontend/js/app.js */

const UI_PORT = 18790;
// Detección dinámica de protocolo WebSocket (SEC-006)
// HTTPS → WSS, HTTP → WS
const wsProtocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
const GATEWAY_URL = `${wsProtocol}//127.0.0.1:${UI_PORT}`;

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
const commandForm = document.getElementById('command-form');

let socket = null;
let reconnectDelay = 1000;
const MAX_RECONNECT_DELAY = 10000;
let typingIndicatorObj = null;
let originalProvider = '';
let awaitingAuth = false;

/**
 * Lee el token de autenticación del Gateway desde localStorage.
 * Si no existe, muestra el modal de autenticación (C12).
 * @returns {string|null}
 */
function getWsToken() {
    const token = sessionStorage.getItem('skyclaw_ws_token');
    return token ? token.trim() : null;
}

// ═══════════════════════════════════════════════════════════════════
// Auth Modal Logic (C12: replaces browser prompt())
// ═══════════════════════════════════════════════════════════════════

const authModal = document.getElementById('auth-modal');
const authForm = document.getElementById('auth-form');
const authTokenInput = document.getElementById('auth-token-input');
const authStatus = document.getElementById('auth-status');
const authSubmitBtn = document.getElementById('auth-submit-btn');

function showAuthModal() {
    if (!authModal) return;
    openModalWithTrap(authModal);
}

function hideAuthModal() {
    if (!authModal) return;
    closeModalWithTrap(authModal);
    if (authStatus) {
        authStatus.textContent = '';
        authStatus.className = 'settings-status';
    }
}

if (authForm) {
    authForm.addEventListener('submit', function (e) {
        e.preventDefault();
        const token = authTokenInput ? authTokenInput.value.trim() : '';
        if (!token) {
            if (authStatus) {
                authStatus.textContent = 'Token requerido.';
                authStatus.className = 'settings-status error';
            }
            return;
        }
        sessionStorage.setItem('skyclaw_ws_token', token);
        hideAuthModal();
        if (authTokenInput) authTokenInput.value = '';
        // Retry connection with the new token
        if (socket) {
            socket.close();
        }
        initConnection();
    });
}

/**
 * Inicializa la conexión con el Gateway
 */
function initConnection() {
    console.log(`[UI] Intentando conectar con el Gateway en ${GATEWAY_URL}...`);

    socket = new WebSocket(GATEWAY_URL);

    socket.onopen = () => {
        console.log('[UI] Conexión establecida con el Gateway. Autenticando...');
        const token = getWsToken();
        if (!token) {
            console.warn('[UI] No hay token guardado. Mostrando modal de autenticación.');
            showAuthModal();
            socket.close();
            return;
        }
        awaitingAuth = true;
        socket.send(JSON.stringify({ type: 'auth', token }));
    };

    socket.onmessage = (event) => {
        try {
            const data = JSON.parse(event.data);

            if (awaitingAuth) {
                if (data.type === 'auth_ok') {
                    awaitingAuth = false;
                    reconnectDelay = 1000;
                    console.log('[UI] Handshake completado. Canal seguro establecido.');
                } else {
                    console.error('[UI] Autenticación rechazada por el Gateway:', data);
                    sessionStorage.removeItem('skyclaw_ws_token');
                    socket.close(4003, 'unauthorized');
                    showAuthModal();
                    if (authStatus) {
                        authStatus.textContent = 'Token rechazado. Ingresa un token valido.';
                        authStatus.className = 'settings-status error';
                    }
                }
                return;
            }

            handleMessage(data);
        } catch (err) {
            if (awaitingAuth) return;
            console.error('[UI] Error al procesar mensaje:', err.message);
            // Si no es JSON, renderizar como texto plano de emergencia
            renderMessage('agent', event.data);
        }
    };

    socket.onclose = () => {
        console.warn(`[UI] Conexión cerrada. Reintentando en ${reconnectDelay}ms...`);
        setBufferingState(true);
        setTimeout(initConnection, reconnectDelay);
        reconnectDelay = Math.min(reconnectDelay * 1.5, MAX_RECONNECT_DELAY);
    };

    socket.onerror = (err) => {
        console.error('[UI] Fallo en el socket:', err.message);
    };
}

/**
 * Procesa los mensajes recibidos del Gateway
 */
function handleMessage(data) {
    try {
        switch (data.type) {
            case 'telemetry':
                handleTelemetry(data.payload || {});
                return;
            case 'agent_state':
                handleAgentState(data.payload || {});
                return;
            case 'chat':
                handleChat(data.payload || {});
                return;
            default:
                // Legacy uppercase types
                handleLegacyMessage(data);
        }
    } catch (err) {
        console.error('[UI] Error en handleMessage:', err);
    }
}

function handleTelemetry(p) {
    if (p.cpu !== undefined) cpuVal.innerText = p.cpu;
    if (p.ram_mb !== undefined) ramVal.innerText = p.ram_mb;
    // Reusa el canal de CustomEvent existente para dashboard.js
    window.dispatchEvent(new CustomEvent('skyclaw-telemetry', {
        detail: { cpu: p.cpu, ram: p.ram_mb, ram_percent: p.ram_percent }
    }));
}

function handleAgentState(p) {
    const nodeTag = p.node ? `[${p.node}]` : '[agente]';
    const text = p.message || '(sin mensaje)';
    renderMessage('system', `${nodeTag} ${text}`);
}

function handleChat(p) {
    removeTypingIndicator();
    renderMessage('agent', p.text || p.message || JSON.stringify(p));
}

function handleLegacyMessage(data) {
    if (data.type === 'STATUS') {
        const content = data.content;
        if (content.includes('[BUFFERING]')) {
            setBufferingState(true);
        } else if (content.includes('[READY]')) {
            setBufferingState(false);
        }
        return;
    }
    if (data.type === 'TELEMETRY') {
        // Camino legacy — backend ya no emite esto, mantenido por seguridad
        const stats = data.content;
        cpuVal.innerText = stats.cpu;
        ramVal.innerText = stats.ram;
        window.dispatchEvent(new CustomEvent('skyclaw-telemetry', { detail: stats }));
        return;
    }
    if (data.type === 'CONFIG_DATA') {
        populateSettingsForm(data.content || {});
        return;
    }
    if (data.type === 'CONFIG_UPDATED') {
        showSettingsStatus(data.success, data.message || (data.success ? 'Guardado.' : 'Error.'));
        return;
    }
    if (data.type === 'RESPONSE' || data.type === 'QUERY' || data.content) {
        removeTypingIndicator();
        renderMessage('agent', data.content || data.message || JSON.stringify(data));
    }
    window.dispatchEvent(new CustomEvent('skyclaw-message', { detail: data }));
}

/**
 * Gestiona el estado visual ante fallos del Daemon (Buffering)
 */
function setBufferingState(isBuffering) {
    if (isBuffering) {
        overlay.classList.remove('is-hidden');
        overlay.classList.add('is-visible');
        commandInput.disabled = true;
        sendBtn.disabled = true;

        statusBadge.className = 'status-badge buffering';
        statusText.innerText = 'ENLAZANDO...';

        telemetryHud.classList.add('disconnected');
        cpuVal.innerText = '--';
        ramVal.innerText = '--';
    } else {
        overlay.classList.remove('is-visible');
        overlay.classList.add('is-hidden');
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

    // Si es del agente, usar Marked.js para el Markdown y purificar
    if (sender === 'agent' && window.marked && window.DOMPurify) {
        const rawContent = marked.parse(content);
        messageDiv.innerHTML = DOMPurify.sanitize(rawContent);
    } else {
        // Fail-closed: if DOMPurify is unavailable, render as safe plain text
        messageDiv.textContent = content;
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
function sendCommand(e) {
    if (e) e.preventDefault();
    const text = commandInput.value.trim();
    if (!text || commandInput.disabled || !socket || socket.readyState !== WebSocket.OPEN) return;

    const payload = {
        type: 'QUERY',
        content: text,
        timestamp: new Date().toISOString(),
        id: crypto.randomUUID ? crypto.randomUUID() : Math.random().toString(36).substring(7)
    };

    // Renderizar mi propia pregunta localmente
    renderMessage('user', text);

    socket.send(JSON.stringify(payload));

    // Mostrar indicador de carga/espera
    showTypingIndicator();

    // Limpiar input
    commandInput.value = '';
}

function showTypingIndicator() {
    removeTypingIndicator();
    typingIndicatorObj = document.createElement('div');
    typingIndicatorObj.className = 'message typing-indicator';
    typingIndicatorObj.innerText = 'El Agente está pensando...';
    chatLog.appendChild(typingIndicatorObj);
    chatLog.scrollTo({ top: chatLog.scrollHeight, behavior: 'smooth' });
}

function removeTypingIndicator() {
    if (typingIndicatorObj && typingIndicatorObj.parentNode) {
        typingIndicatorObj.parentNode.removeChild(typingIndicatorObj);
        typingIndicatorObj = null;
    }
}

// ═══════════════════════════════════════════════════════════════════
// Settings Modal Logic
// ═══════════════════════════════════════════════════════════════════

const settingsBtn = document.getElementById('settings-btn');
const settingsModal = document.getElementById('settings-modal');
const settingsForm = document.getElementById('settings-form');
const modalClose = document.getElementById('modal-close');
const settingsStatus = document.getElementById('settings-status');
const saveConfigBtn = document.getElementById('save-config-btn');

let isFormDirty = false;

// Open modal & request current config (C05: with focus trap)
if (settingsBtn) {
    settingsBtn.addEventListener('click', () => {
        openModalWithTrap(settingsModal);
        requestConfig();
    });
}

// Close modal: X button, overlay click, ESC key
if (modalClose) {
    modalClose.addEventListener('click', closeSettingsModal);
}

if (settingsModal) {
    settingsModal.addEventListener('click', (e) => {
        if (e.target === settingsModal) closeSettingsModal();
    });
}

document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
        // Close whichever modal is visible
        if (settingsModal && settingsModal.classList.contains('is-visible')) {
            closeSettingsModal();
        }
        if (authModal && authModal.classList.contains('is-visible')) {
            // Don't close auth modal on Escape if no token stored (required)
            if (getWsToken()) hideAuthModal();
        }
    }
});

function closeSettingsModal() {
    if (isFormDirty) {
        if (!confirm('Hay cambios sin guardar. ¿Descartar cambios?')) return;
    }
    isFormDirty = false;
    closeModalWithTrap(settingsModal);
    clearSettingsStatus();
    // Clear and reset password inputs on close for security
    document.querySelectorAll('#settings-form input[type="password"], #settings-form input[type="text"][data-was-password]').forEach(input => {
        input.value = '';
        if (input.dataset.wasPassword) {
            input.type = 'password';
            delete input.dataset.wasPassword;
            const btn = input.nextElementSibling;
            if (btn && btn.classList.contains('password-toggle')) {
                btn.setAttribute('aria-pressed', 'false');
                btn.setAttribute('aria-label', 'Mostrar contraseña');
                btn.innerHTML = EYE_ICON;
            }
        }
    });
}

/**
 * Request current config from the backend via WebSocket
 */
function requestConfig() {
    if (!socket || socket.readyState !== WebSocket.OPEN) return;
    const payload = {
        type: 'GET_CONFIG',
        id: crypto.randomUUID ? crypto.randomUUID() : Math.random().toString(36).substring(7),
        timestamp: new Date().toISOString()
    };
    socket.send(JSON.stringify(payload));
}

/**
 * Populate the settings form with config data from the backend
 */
function populateSettingsForm(config) {
    // Set LLM provider select
    const providerSelect = document.getElementById('cfg-llm-provider');
    if (providerSelect && config.llm_provider) {
        providerSelect.value = config.llm_provider;
        originalProvider = config.llm_provider;
    }

    // Set Telegram Chat ID
    const chatIdInput = document.getElementById('cfg-tg-chatid');
    if (chatIdInput) {
        chatIdInput.value = config.telegram_chat_id || '';
    }

    // Update key status indicators
    updateKeyStatus('status-llm-key', config.has_llm_key);
    updateKeyStatus('status-nexus-key', config.has_nexus_key);
    updateKeyStatus('status-tg-token', config.has_telegram_token);
}

function updateKeyStatus(elementId, hasKey) {
    const el = document.getElementById(elementId);
    if (!el) return;
    if (hasKey) {
        el.className = 'key-status configured';
        el.textContent = 'Configurada';
    } else {
        el.className = 'key-status not-configured';
        el.textContent = 'Sin clave';
    }
}

/**
 * Handle settings form submission
 */
if (settingsForm) {
    settingsForm.addEventListener('submit', (e) => {
        e.preventDefault();
        if (!socket || socket.readyState !== WebSocket.OPEN) {
            showSettingsStatus(false, 'Sin conexion al Gateway.');
            return;
        }

        const content = {};
        const provider = document.getElementById('cfg-llm-provider').value;
        if (provider && provider !== originalProvider) content.llm_provider = provider;

        // Only include secret fields if user typed something
        const llmKey = document.getElementById('cfg-llm-key').value.trim();
        if (llmKey) content.llm_api_key = llmKey;

        const nexusKey = document.getElementById('cfg-nexus-key').value.trim();
        if (nexusKey) content.nexus_api_key = nexusKey;

        const tgToken = document.getElementById('cfg-tg-token').value.trim();
        if (tgToken) content.telegram_bot_token = tgToken;

        const tgChatId = document.getElementById('cfg-tg-chatid').value.trim();
        if (tgChatId) content.telegram_chat_id = tgChatId;

        const payload = {
            type: 'UPDATE_CONFIG',
            id: crypto.randomUUID ? crypto.randomUUID() : Math.random().toString(36).substring(7),
            content: content,
            timestamp: new Date().toISOString()
        };

        socket.send(JSON.stringify(payload));

        // Disable save button until server responds (re-enabled by showSettingsStatus)
        saveConfigBtn.disabled = true;
        saveConfigBtn.textContent = 'GUARDANDO...';
    });
}

/**
 * Show feedback status in the settings modal
 */
function showSettingsStatus(success, message) {
    if (!settingsStatus) return;
    settingsStatus.textContent = message;
    settingsStatus.className = 'settings-status ' + (success ? 'success' : 'error');

    // Re-enable save button on response
    if (saveConfigBtn) {
        saveConfigBtn.disabled = false;
        saveConfigBtn.textContent = 'GUARDAR CONFIGURACIÓN';
    }

    // Clear password inputs on success
    if (success) {
        isFormDirty = false;
        document.querySelectorAll('#settings-form input[type="password"], #settings-form input[type="text"][data-was-password]').forEach(input => {
            input.value = '';
            if (input.dataset.wasPassword) {
                input.type = 'password';
                delete input.dataset.wasPassword;
                const btn = input.nextElementSibling;
                if (btn && btn.classList.contains('password-toggle')) {
                    btn.setAttribute('aria-pressed', 'false');
                    btn.setAttribute('aria-label', 'Mostrar contraseña');
                    btn.innerHTML = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false"><path d="M2 12s3-7 10-7 10 7 10 7-3 7-10 7-10-7-10-7z"/><circle cx="12" cy="12" r="3"/></svg>';
                }
            }
        });
        // Refresh config data to update key status indicators
        requestConfig();
    }

    // Auto-clear message after 8 seconds
    setTimeout(clearSettingsStatus, 8000);
}

function clearSettingsStatus() {
    if (settingsStatus) {
        settingsStatus.textContent = '';
        settingsStatus.className = 'settings-status';
    }
}

// M08: Mark form dirty on any input change
if (settingsForm) {
    settingsForm.addEventListener('input', () => { isFormDirty = true; });
}

// M08: Warn on page unload with unsaved changes
window.addEventListener('beforeunload', (e) => {
    if (isFormDirty) {
        e.preventDefault();
        e.returnValue = '';
    }
});

// ═══════════════════════════════════════════════════════════════════
// M06: Password toggle — show/hide password fields
// ═══════════════════════════════════════════════════════════════════

const EYE_ICON = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false"><path d="M2 12s3-7 10-7 10 7 10 7-3 7-10 7-10-7-10-7z"/><circle cx="12" cy="12" r="3"/></svg>';
const EYE_OFF_ICON = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false"><path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/><line x1="1" y1="1" x2="23" y2="23"/></svg>';

document.querySelectorAll('.password-toggle').forEach(btn => {
    btn.addEventListener('click', () => {
        const input = btn.previousElementSibling;
        if (!input || input.tagName !== 'INPUT') return;
        const isHidden = input.type === 'password';
        input.type = isHidden ? 'text' : 'password';
        if (isHidden) {
            input.dataset.wasPassword = '1';
        } else {
            delete input.dataset.wasPassword;
        }
        btn.setAttribute('aria-pressed', isHidden ? 'true' : 'false');
        btn.setAttribute('aria-label', isHidden ? 'Ocultar contraseña' : 'Mostrar contraseña');
        btn.innerHTML = isHidden ? EYE_OFF_ICON : EYE_ICON;
    });
});

// ═══════════════════════════════════════════════════════════════════
// C05: Focus Trap for Modals
// ═══════════════════════════════════════════════════════════════════

let lastFocusedElement = null;

/**
 * Traps Tab key focus within a modal container.
 * @param {HTMLElement} modal - The modal overlay element
 */
function trapFocus(modal) {
    const focusableSelectors = 'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';
    const focusableElements = modal.querySelectorAll(focusableSelectors);
    if (focusableElements.length === 0) return;

    const firstFocusable = focusableElements[0];
    const lastFocusable = focusableElements[focusableElements.length - 1];

    modal._focusTrapHandler = function (e) {
        if (e.key !== 'Tab') return;

        if (e.shiftKey) {
            // Shift+Tab: if on first element, wrap to last
            if (document.activeElement === firstFocusable) {
                e.preventDefault();
                lastFocusable.focus();
            }
        } else {
            // Tab: if on last element, wrap to first
            if (document.activeElement === lastFocusable) {
                e.preventDefault();
                firstFocusable.focus();
            }
        }
    };

    modal.addEventListener('keydown', modal._focusTrapHandler);
}

function releaseFocusTrap(modal) {
    if (modal && modal._focusTrapHandler) {
        modal.removeEventListener('keydown', modal._focusTrapHandler);
        modal._focusTrapHandler = null;
    }
}

/**
 * Opens a modal with focus trap and saves the trigger element.
 * @param {HTMLElement} modal - The modal to open
 */
function openModalWithTrap(modal) {
    if (!modal) return;
    lastFocusedElement = document.activeElement;
    modal.classList.remove('is-hidden');
    modal.classList.add('is-visible');
    trapFocus(modal);
    // Focus first focusable element inside the modal
    const firstInput = modal.querySelector('input:not([disabled]), select:not([disabled]), button:not([disabled])');
    if (firstInput) firstInput.focus();
}

/**
 * Closes a modal, releases focus trap, and restores focus.
 * @param {HTMLElement} modal - The modal to close
 */
function closeModalWithTrap(modal) {
    if (!modal) return;
    const content = modal.querySelector('.modal-content');

    const finish = () => {
        if (content) content.classList.remove('is-closing');
        modal.classList.remove('is-visible');
        modal.classList.add('is-hidden');
        releaseFocusTrap(modal);
        if (lastFocusedElement) {
            lastFocusedElement.focus();
            lastFocusedElement = null;
        }
    };

    if (content) {
        content.classList.add('is-closing');
        // Fallback in case animationend never fires (e.g. prefers-reduced-motion)
        const fallback = setTimeout(finish, 250);
        content.addEventListener('animationend', function handler() {
            clearTimeout(fallback);
            content.removeEventListener('animationend', handler);
            finish();
        });
    } else {
        finish();
    }
}

// Event Listeners
if (commandForm) {
    commandForm.addEventListener('submit', sendCommand);
} else {
    sendBtn.addEventListener('click', sendCommand);
    commandInput.addEventListener('keypress', (e) => {
        if (e.key === 'Enter') sendCommand(e);
    });
}

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

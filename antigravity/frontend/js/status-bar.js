/* ═══════════════════════════════════════════════════════════════════════════════════
   SKY-CLAW — OPERATIONS HUB · Status bar binder
   Reactive binder for the bottom status bar:
     • Connection indicator (coloured dot + textual state, in Spanish).
     • HITL alert counter (pluralised text from state.conflicts.length).

   Pure helpers (connectionLabel, connectionDotClass, hitlLabel) are exported
   so tests can verify the label strategy without a DOM.
   ═══════════════════════════════════════════════════════════════════════════════════ */

/** CSS modifier classes the dot cycles through. */
export const DOT_CLASSES = Object.freeze([
    'ops-statusbar__dot--ok',
    'ops-statusbar__dot--warn',
    'ops-statusbar__dot--err',
]);


/**
 * Map a connection status object to its Spanish user-facing label.
 *
 * @param {{status?: string, attempts?: number, lastError?: string|null}} conn
 * @returns {string}
 */
export function connectionLabel(conn) {
    if (!conn || typeof conn !== 'object') return 'Desconectado';
    const status = String(conn.status ?? 'disconnected');
    switch (status) {
        case 'connected':    return 'Conectado';
        case 'connecting':   return 'Conectando…';
        case 'reconnecting': {
            const n = Number.isFinite(conn.attempts) ? conn.attempts : 0;
            return n > 0 ? `Reconectando (intento ${n})` : 'Reconectando…';
        }
        case 'closed':       return 'Cerrado';
        case 'disconnected':
        default:             return 'Desconectado';
    }
}


/**
 * Map a connection status to the dot's CSS modifier class.
 *
 * @param {{status?: string}} conn
 * @returns {string|null} null → neutral (no modifier).
 */
export function connectionDotClass(conn) {
    const status = String(conn?.status ?? 'disconnected');
    switch (status) {
        case 'connected':    return 'ops-statusbar__dot--ok';
        case 'connecting':
        case 'reconnecting': return 'ops-statusbar__dot--warn';
        case 'disconnected':
        case 'closed':       return 'ops-statusbar__dot--err';
        default:             return null;
    }
}


/**
 * Pluralise the HITL alert counter for display.
 *
 * @param {number} count
 * @returns {string}
 */
export function hitlLabel(count) {
    const n = Number.isFinite(count) ? count : 0;
    if (n === 0) return '0 alertas HITL';
    if (n === 1) return '1 alerta HITL';
    return `${n} alertas HITL`;
}


/**
 * Binder for the bottom status bar. No rAF coalescing is required here —
 * connection + conflicts mutations are bounded (a few per second at most).
 *
 * Example:
 *     new StatusBarBinder({
 *         state: hub.state,
 *         elements: {
 *             dot:      document.querySelector('#ops-conn-indicator .ops-statusbar__dot'),
 *             connText: document.getElementById('ops-conn-text'),
 *             hitlText: document.getElementById('ops-hitl-text'),
 *         },
 *     });
 */
export class StatusBarBinder {
    /**
     * @param {object} opts
     * @param {import('./appstate.js').AppState} opts.state
     * @param {object} opts.elements
     * @param {HTMLElement} [opts.elements.dot]      Status dot.
     * @param {HTMLElement} [opts.elements.connText] Textual connection label.
     * @param {HTMLElement} [opts.elements.hitlText] HITL counter text.
     * @param {HTMLElement} [opts.elements.indicator] Optional wrapper for aria-live.
     */
    constructor({ state, elements = {} }) {
        if (!state || typeof state.subscribe !== 'function') {
            throw new TypeError('StatusBarBinder requires an AppState-like object');
        }
        this._state = state;
        this._el = elements;

        this._unsubConnection = this._state.subscribe('connection', (conn) => this._onConnection(conn));
        this._unsubConflicts  = this._state.subscribe('conflicts',  (list) => this._onConflicts(list));

        // Initial paint from current snapshots.
        this._onConnection(this._state.connection);
        this._onConflicts(this._state.conflicts);
    }

    /** Unsubscribe. */
    dispose() {
        if (this._unsubConnection) this._unsubConnection();
        if (this._unsubConflicts)  this._unsubConflicts();
    }

    // ──────────────────────────── Internals ────────────────────────────────

    /** @private */
    _onConnection(conn) {
        if (this._el.connText) {
            this._el.connText.textContent = connectionLabel(conn);
        }
        if (this._el.dot) {
            const dot = this._el.dot;
            const cls = connectionDotClass(conn);
            // Strip any prior modifier, then add the current one.
            if (dot.classList && typeof dot.classList.remove === 'function') {
                for (const c of DOT_CLASSES) dot.classList.remove(c);
                if (cls) dot.classList.add(cls);
            } else {
                // Fallback for stubs without classList — rewrite className.
                const baseClass = 'ops-statusbar__dot';
                dot.className = cls ? `${baseClass} ${cls}` : baseClass;
            }
        }
        if (this._el.indicator) {
            this._el.indicator.setAttribute('aria-label', connectionLabel(conn));
        }
    }

    /** @private */
    _onConflicts(list) {
        if (!this._el.hitlText) return;
        const count = Array.isArray(list) ? list.length : 0;
        this._el.hitlText.textContent = hitlLabel(count);
    }
}

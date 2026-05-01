/* ═══════════════════════════════════════════════════════════════════════════════════
   SKY-CLAW — OPERATIONS HUB · WebSocket client
   Thin wrapper around the browser WebSocket API that adds:
     • Exponential-backoff auto-reconnect (capped at MAX_BACKOFF_MS).
     • Light event emitter API: on('open'|'close'|'error'|'frame'|'reconnecting').
     • JSON-safe send() that tolerates a disconnected socket (drops + warns).
     • Optional application-level ping that complements aiohttp's protocol ping.

   The client is UI-agnostic: callers hook `on('frame', fn)` and route event_type
   → AppState mutations in operations-hub.js (the wiring layer).
   ═══════════════════════════════════════════════════════════════════════════════════ */

/** Minimum initial backoff (ms). */
const BASE_BACKOFF_MS = 1000;

/** Maximum backoff cap (ms). */
const MAX_BACKOFF_MS = 30000;

/** Heartbeat ping interval (ms) — belt & suspenders alongside aiohttp's PING frame. */
const DEFAULT_PING_INTERVAL_MS = 20000;

/** Backoff jitter fraction (± this ratio of the computed delay). */
const BACKOFF_JITTER = 0.2;


/**
 * Resolve the WebSocket URL for a path relative to the current origin.
 * Upgrades `http:` → `ws:` and `https:` → `wss:` automatically.
 *
 * @param {string} path e.g. '/api/status'
 * @param {Location} [loc] defaults to window.location
 * @returns {string}
 */
export function buildWsUrl(path, loc = (typeof window !== 'undefined' ? window.location : null)) {
    if (!loc) {
        throw new Error('buildWsUrl: no location available (non-browser environment?)');
    }
    const proto = loc.protocol === 'https:' ? 'wss:' : 'ws:';
    const normalised = path.startsWith('/') ? path : `/${path}`;
    return `${proto}//${loc.host}${normalised}`;
}


/**
 * WebSocket client with auto-reconnect.
 *
 * Example:
 *     const client = new WebSocketClient(buildWsUrl('/api/status'));
 *     client.on('frame', (frame) => console.log(frame.event_type));
 *     client.connect();
 */
export class WebSocketClient {
    /**
     * @param {string} url Fully-qualified `ws://` or `wss://` URL.
     * @param {object} [options]
     * @param {number} [options.baseBackoffMs=1000]
     * @param {number} [options.maxBackoffMs=30000]
     * @param {number} [options.pingIntervalMs=20000]  Set to 0 to disable app-level ping.
     * @param {typeof WebSocket} [options.wsFactory]   Override for testing / SSR.
     */
    constructor(url, {
        baseBackoffMs = BASE_BACKOFF_MS,
        maxBackoffMs = MAX_BACKOFF_MS,
        pingIntervalMs = DEFAULT_PING_INTERVAL_MS,
        wsFactory = (typeof WebSocket !== 'undefined' ? WebSocket : null),
    } = {}) {
        if (!url || typeof url !== 'string') {
            throw new TypeError('WebSocketClient requires a url string');
        }
        if (!wsFactory) {
            throw new Error('WebSocketClient: no WebSocket constructor available');
        }
        this._url = url;
        this._baseBackoffMs = baseBackoffMs;
        this._maxBackoffMs = maxBackoffMs;
        this._pingIntervalMs = pingIntervalMs;
        this._WebSocket = wsFactory;

        /** @private */ this._ws = null;
        /** @private */ this._listeners = new Map();
        /** @private */ this._reconnectAttempts = 0;
        /** @private */ this._reconnectTimer = null;
        /** @private */ this._pingTimer = null;
        /** @private */ this._intentionalClose = false;
    }

    // ───────────────────────────── Public API ───────────────────────────────

    /** Open the socket (idempotent while already connected/connecting). */
    connect() {
        if (this._ws && (this._ws.readyState === this._WebSocket.OPEN || this._ws.readyState === this._WebSocket.CONNECTING)) {
            return;
        }
        this._intentionalClose = false;
        this._clearReconnectTimer();
        this._emit('connecting', { attempts: this._reconnectAttempts });

        let ws;
        try {
            ws = new this._WebSocket(this._url);
        } catch (err) {
            this._emit('error', { error: err });
            this._scheduleReconnect();
            return;
        }
        this._ws = ws;

        ws.addEventListener('open', () => this._handleOpen());
        ws.addEventListener('message', (ev) => this._handleMessage(ev));
        ws.addEventListener('close', (ev) => this._handleClose(ev));
        ws.addEventListener('error', (ev) => this._emit('error', { event: ev }));
    }

    /** Close the socket and stop reconnecting. */
    disconnect() {
        this._intentionalClose = true;
        this._clearReconnectTimer();
        this._clearPingTimer();
        if (this._ws) {
            try {
                this._ws.close(1000, 'client-disconnect');
            } catch {
                // Ignore: socket already closed or in a bad state.
            }
        }
        this._ws = null;
    }

    /**
     * Send a JSON-serialisable object over the socket.
     * Returns true if the frame was queued, false if the socket was not open.
     *
     * @param {object} obj
     * @returns {boolean}
     */
    send(obj) {
        if (!this._ws || this._ws.readyState !== this._WebSocket.OPEN) {
            // eslint-disable-next-line no-console
            console.warn('[WebSocketClient] send() called while not OPEN, dropping frame');
            return false;
        }
        try {
            this._ws.send(JSON.stringify(obj));
            return true;
        } catch (err) {
            // eslint-disable-next-line no-console
            console.error('[WebSocketClient] send() failed:', err);
            return false;
        }
    }

    /** Send an application-level ping. */
    ping() {
        return this.send({ action: 'ping' });
    }

    /**
     * Register a listener. Returns an unsubscribe function.
     * Events:
     *   - 'connecting'   {attempts}
     *   - 'open'         {attempts}
     *   - 'frame'        parsed JSON payload from the server
     *   - 'close'        {code, reason, wasClean}
     *   - 'error'        {error?, event?}
     *   - 'reconnecting' {attempts, delayMs}
     *
     * @param {string} event
     * @param {Function} callback
     * @returns {() => void}
     */
    on(event, callback) {
        if (typeof callback !== 'function') {
            throw new TypeError('WebSocketClient.on requires a function callback');
        }
        let bucket = this._listeners.get(event);
        if (!bucket) {
            bucket = new Set();
            this._listeners.set(event, bucket);
        }
        bucket.add(callback);
        return () => { bucket.delete(callback); };
    }

    /** @returns {'CONNECTING'|'OPEN'|'CLOSING'|'CLOSED'|'NONE'} */
    get readyStateLabel() {
        if (!this._ws) return 'NONE';
        switch (this._ws.readyState) {
            case this._WebSocket.CONNECTING: return 'CONNECTING';
            case this._WebSocket.OPEN: return 'OPEN';
            case this._WebSocket.CLOSING: return 'CLOSING';
            case this._WebSocket.CLOSED: return 'CLOSED';
            default: return 'NONE';
        }
    }

    // ──────────────────────────── Internals ─────────────────────────────────

    /** @private */
    _emit(event, payload) {
        const bucket = this._listeners.get(event);
        if (!bucket || bucket.size === 0) return;
        for (const fn of Array.from(bucket)) {
            try {
                fn(payload);
            } catch (err) {
                // eslint-disable-next-line no-console
                console.error('[WebSocketClient] listener for "%s" threw:', event, err);
            }
        }
    }

    /** @private */
    _handleOpen() {
        const attempts = this._reconnectAttempts;
        this._reconnectAttempts = 0;
        this._startPingTimer();
        this._emit('open', { attempts });
    }

    /** @private */
    _handleMessage(ev) {
        if (typeof ev.data !== 'string') {
            // Binary frames are not part of the Operations Hub protocol.
            return;
        }
        let frame;
        try {
            frame = JSON.parse(ev.data);
        } catch (err) {
            // eslint-disable-next-line no-console
            console.warn('[WebSocketClient] dropped non-JSON frame:', err);
            return;
        }
        this._emit('frame', frame);
    }

    /** @private */
    _handleClose(ev) {
        this._clearPingTimer();
        this._emit('close', {
            code: ev?.code,
            reason: ev?.reason,
            wasClean: ev?.wasClean,
        });
        this._ws = null;
        if (!this._intentionalClose) {
            this._scheduleReconnect();
        }
    }

    /** @private */
    _scheduleReconnect() {
        if (this._intentionalClose) return;
        this._clearReconnectTimer();
        const attempts = this._reconnectAttempts;
        const exponential = Math.min(this._baseBackoffMs * (2 ** attempts), this._maxBackoffMs);
        const jitter = exponential * BACKOFF_JITTER * (Math.random() * 2 - 1);
        const delayMs = Math.max(0, Math.round(exponential + jitter));
        this._reconnectAttempts = attempts + 1;
        this._emit('reconnecting', { attempts: this._reconnectAttempts, delayMs });
        this._reconnectTimer = setTimeout(() => {
            this._reconnectTimer = null;
            this.connect();
        }, delayMs);
    }

    /** @private */
    _clearReconnectTimer() {
        if (this._reconnectTimer !== null) {
            clearTimeout(this._reconnectTimer);
            this._reconnectTimer = null;
        }
    }

    /** @private */
    _startPingTimer() {
        this._clearPingTimer();
        if (!this._pingIntervalMs || this._pingIntervalMs <= 0) return;
        this._pingTimer = setInterval(() => {
            this.ping();
        }, this._pingIntervalMs);
    }

    /** @private */
    _clearPingTimer() {
        if (this._pingTimer !== null) {
            clearInterval(this._pingTimer);
            this._pingTimer = null;
        }
    }
}

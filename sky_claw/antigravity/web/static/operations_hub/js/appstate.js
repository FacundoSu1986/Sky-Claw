/* ═══════════════════════════════════════════════════════════════════════════════════
   SKY-CLAW — OPERATIONS HUB · AppState
   Reactive client-side store built on the Observer pattern (no framework).
   Exposes a small, typed-like API that the WebSocket client and UI binders
   consume:
     - appendLog(entry)    → ring buffer (O(1) push, bounded memory)
     - updateProcess(id, patch)
     - updateTelemetry(patch)
     - addConflict(c) / resolveConflict(id)
     - setConnection(status, attempts)

   Every mutation fans out to subscribers via key-scoped observers so Phase 4/5
   DOM binders can re-render only what changed.
   ═══════════════════════════════════════════════════════════════════════════════════ */

/**
 * Fixed-capacity circular buffer.
 *
 * Used by AppState to cap the log stream at LOGS_CAPACITY entries without
 * incurring Array.shift's O(n) cost on every append. The oldest entry is
 * overwritten once the buffer is full.
 */
export class RingBuffer {
    /**
     * @param {number} capacity Maximum number of retained entries (must be > 0).
     */
    constructor(capacity) {
        if (!Number.isFinite(capacity) || capacity <= 0) {
            throw new RangeError(`RingBuffer capacity must be a positive finite number, got ${capacity}`);
        }
        this._capacity = capacity | 0;
        this._buffer = new Array(this._capacity);
        this._head = 0;   // index of the oldest entry
        this._size = 0;   // number of entries currently stored
    }

    /**
     * Append an item; evicts the oldest entry when the buffer is full.
     * Returns the evicted entry (or undefined if none was dropped).
     *
     * @param {*} item
     * @returns {*}
     */
    push(item) {
        const writeIndex = (this._head + this._size) % this._capacity;
        let evicted;
        if (this._size < this._capacity) {
            this._size += 1;
        } else {
            evicted = this._buffer[this._head];
            this._head = (this._head + 1) % this._capacity;
        }
        this._buffer[writeIndex] = item;
        return evicted;
    }

    /**
     * Return a snapshot array of entries in insertion order (oldest → newest).
     * Safe to mutate; the internal buffer is not aliased.
     *
     * @returns {Array}
     */
    toArray() {
        const out = new Array(this._size);
        for (let i = 0; i < this._size; i += 1) {
            out[i] = this._buffer[(this._head + i) % this._capacity];
        }
        return out;
    }

    /** Remove all entries. */
    clear() {
        this._buffer.fill(undefined);
        this._head = 0;
        this._size = 0;
    }

    /** @returns {number} Current number of entries held. */
    get length() {
        return this._size;
    }

    /** @returns {number} Configured maximum entries. */
    get capacity() {
        return this._capacity;
    }
}


/** Log levels recognised by the Orbe de Visión filters. */
export const LOG_LEVELS = Object.freeze(['debug', 'info', 'success', 'warning', 'error']);

/** Default ring-buffer size for the log stream. */
export const DEFAULT_LOG_CAPACITY = 5000;


/**
 * Reactive store for the Operations Hub.
 *
 * Subscribers are scoped per key so a logs burst does not wake up the
 * telemetry binder. Keys in use:
 *   - 'logs'       → RingBuffer snapshot (array)
 *   - 'processes'  → Map<string, object>
 *   - 'telemetry'  → {cpu, memory, activeProcesses, uptimeMs, ...}
 *   - 'conflicts'  → Array<object>
 *   - 'connection' → {status, attempts, lastError?}
 */
export class AppState {
    /**
     * @param {{logsCapacity?: number}} [options]
     */
    constructor({ logsCapacity = DEFAULT_LOG_CAPACITY } = {}) {
        /** @private */
        this._listeners = new Map();
        /** @private */
        this._logs = new RingBuffer(logsCapacity);
        /** @private */
        this._processes = new Map();
        /** @private */
        this._telemetry = {
            cpu: 0,
            memory: 0,
            memoryMax: 0,
            activeProcesses: 0,
            uptimeMs: 0,
        };
        /** @private */
        this._conflicts = [];
        /** @private */
        this._connection = {
            status: 'disconnected', // 'disconnected' | 'connecting' | 'connected' | 'reconnecting' | 'closed'
            attempts: 0,
            lastError: null,
        };
    }

    // ───────────────────────────── Observer API ─────────────────────────────

    /**
     * Subscribe to mutations on a given key. Returns an unsubscribe function.
     *
     * @param {string} key
     * @param {(value: any) => void} callback
     * @returns {() => void}
     */
    subscribe(key, callback) {
        if (typeof callback !== 'function') {
            throw new TypeError('AppState.subscribe requires a function callback');
        }
        let bucket = this._listeners.get(key);
        if (!bucket) {
            bucket = new Set();
            this._listeners.set(key, bucket);
        }
        bucket.add(callback);
        return () => { bucket.delete(callback); };
    }

    /** @private */
    _notify(key, value) {
        const bucket = this._listeners.get(key);
        if (!bucket || bucket.size === 0) return;
        // Iterate on a snapshot so unsubscribing mid-notify is safe.
        for (const fn of Array.from(bucket)) {
            try {
                fn(value);
            } catch (err) {
                // Never let one subscriber break the fan-out.
                // eslint-disable-next-line no-console
                console.error('[AppState] subscriber for key "%s" threw:', key, err);
            }
        }
    }

    // ─────────────────────────────── Logs ───────────────────────────────────

    /**
     * Append a single log entry. Unknown levels fall back to 'info'.
     * The entry is frozen so subscribers can safely alias it.
     *
     * @param {{level?: string, message: string, source?: string, timestampMs?: number, topic?: string, meta?: object}} entry
     */
    appendLog(entry) {
        if (!entry || typeof entry !== 'object') return;
        const normalised = Object.freeze({
            level: LOG_LEVELS.includes(entry.level) ? entry.level : 'info',
            message: String(entry.message ?? ''),
            source: entry.source ?? null,
            timestampMs: Number.isFinite(entry.timestampMs) ? entry.timestampMs : Date.now(),
            topic: entry.topic ?? null,
            meta: entry.meta ?? null,
        });
        this._logs.push(normalised);
        this._notify('logs', this._logs.toArray());
    }

    /** Drop every log entry and notify subscribers with an empty snapshot. */
    clearLogs() {
        this._logs.clear();
        this._notify('logs', []);
    }

    /** @returns {Array} Snapshot of log entries, oldest first. */
    get logs() {
        return this._logs.toArray();
    }

    // ───────────────────────────── Processes ────────────────────────────────

    /**
     * Merge-patch a process entry by id and notify.
     * Passing `state: 'finished'` (or any terminal state) keeps the entry
     * so the UI can show a final status; use removeProcess to drop it.
     *
     * @param {string} id
     * @param {object} patch
     */
    updateProcess(id, patch) {
        if (!id) return;
        const prev = this._processes.get(id) ?? { id };
        const next = Object.freeze({ ...prev, ...(patch ?? {}), id });
        this._processes.set(id, next);
        this._notify('processes', new Map(this._processes));
    }

    /**
     * Remove a process entry by id.
     * @param {string} id
     */
    removeProcess(id) {
        if (!id || !this._processes.has(id)) return;
        this._processes.delete(id);
        this._notify('processes', new Map(this._processes));
    }

    /** @returns {Map<string, object>} Clone of the current process map. */
    get processes() {
        return new Map(this._processes);
    }

    // ───────────────────────────── Telemetry ────────────────────────────────

    /**
     * Merge-patch the telemetry object and notify subscribers.
     * Unknown keys are preserved so producers can evolve without code changes.
     *
     * @param {object} patch
     */
    updateTelemetry(patch) {
        if (!patch || typeof patch !== 'object') return;
        this._telemetry = Object.freeze({ ...this._telemetry, ...patch });
        this._notify('telemetry', this._telemetry);
    }

    /** @returns {object} Frozen snapshot of telemetry. */
    get telemetry() {
        return this._telemetry;
    }

    // ─────────────────────────────── HITL ───────────────────────────────────

    /**
     * Add a conflict / HITL alert. Duplicate IDs are updated in place.
     *
     * @param {{id: string, [key: string]: any}} conflict
     */
    addConflict(conflict) {
        if (!conflict || typeof conflict !== 'object' || !conflict.id) return;
        const existingIdx = this._conflicts.findIndex((c) => c.id === conflict.id);
        if (existingIdx >= 0) {
            this._conflicts[existingIdx] = Object.freeze({ ...this._conflicts[existingIdx], ...conflict });
        } else {
            this._conflicts.push(Object.freeze({ ...conflict }));
        }
        this._notify('conflicts', [...this._conflicts]);
    }

    /**
     * Remove a conflict entry by id.
     * @param {string} id
     */
    resolveConflict(id) {
        if (!id) return;
        const nextLen = this._conflicts.length;
        this._conflicts = this._conflicts.filter((c) => c.id !== id);
        if (this._conflicts.length !== nextLen) {
            this._notify('conflicts', [...this._conflicts]);
        }
    }

    /** @returns {Array} Clone of the conflict queue. */
    get conflicts() {
        return [...this._conflicts];
    }

    // ───────────────────────────── Connection ───────────────────────────────

    /**
     * Update the WebSocket connection indicator.
     *
     * @param {'disconnected'|'connecting'|'connected'|'reconnecting'|'closed'} status
     * @param {{attempts?: number, lastError?: string|null}} [extras]
     */
    setConnection(status, extras = {}) {
        this._connection = Object.freeze({
            status,
            attempts: Number.isFinite(extras.attempts) ? extras.attempts : this._connection.attempts,
            lastError: extras.lastError ?? null,
        });
        this._notify('connection', this._connection);
    }

    /** @returns {object} Frozen snapshot of connection state. */
    get connection() {
        return this._connection;
    }
}

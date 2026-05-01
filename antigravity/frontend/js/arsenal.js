/* ═══════════════════════════════════════════════════════════════════════════════════
   SKY-CLAW — OPERATIONS HUB · Arsenal binder
   Wires the right-panel command buttons to the WebSocket client and to
   `state.processes` / `state.conflicts`, with inflight debouncing to prevent
   double-clicks.

   Directive (Fase 5):
     • While a command is in flight (server has not ack'd completion yet) the
       button is disabled. The mapping of command → "am I running?" uses
       state.processes: any process whose state is active (started/running/…)
       and whose `command` field (or topic suffix) matches, debounces it.
     • Special rules:
         - `approve_conflict` requires at least one queued conflict.
         - `cancel_process`   requires at least one running process.
     • A 5s safety timer force-releases the inflight lock if no backend ack
       arrives, so a dropped WebSocket frame can never permanently soft-brick
       a button.

   The binder never mutates the DOM outside the buttons it received.
   ═══════════════════════════════════════════════════════════════════════════════════ */

/** Default auto-release timeout for inflight locks (ms). */
export const INFLIGHT_TIMEOUT_MS = 5000;

/** Process states that count as "active / in flight". */
export const ACTIVE_PROCESS_STATES = Object.freeze(new Set([
    'queued', 'pending', 'started', 'running', 'in_progress', 'active',
]));

/** Process states that count as terminal (clear the inflight lock). */
export const TERMINAL_PROCESS_STATES = Object.freeze(new Set([
    'finished', 'completed', 'done', 'success', 'failed', 'error', 'cancelled', 'canceled',
]));


/**
 * Return true when any process in the map is in a non-terminal state.
 *
 * @param {Map<string, object>|Iterable<[string, object]>} processes
 * @returns {boolean}
 */
export function hasActiveProcess(processes) {
    if (!processes) return false;
    const entries = (typeof processes.values === 'function') ? processes.values() : [];
    for (const proc of entries) {
        if (!proc) continue;
        const st = String(proc.state ?? '').toLowerCase();
        if (ACTIVE_PROCESS_STATES.has(st)) return true;
        if (!TERMINAL_PROCESS_STATES.has(st) && st !== '' && st !== 'unknown') return true;
    }
    return false;
}


/**
 * Return true when any process in the map belongs to the given command.
 * Matches via `proc.command`, `proc.topic`, or `proc.id` prefix.
 *
 * @param {Map<string, object>|Iterable<[string, object]>} processes
 * @param {string} command
 * @returns {boolean}
 */
export function isCommandActive(processes, command) {
    if (!processes || !command) return false;
    const entries = (typeof processes.values === 'function') ? processes.values() : [];
    for (const proc of entries) {
        if (!proc) continue;
        const st = String(proc.state ?? '').toLowerCase();
        const matchesCommand = (
            proc.command === command
            || (typeof proc.topic === 'string' && proc.topic.includes(command))
            || (typeof proc.id === 'string' && proc.id.startsWith(command))
        );
        if (!matchesCommand) continue;
        if (TERMINAL_PROCESS_STATES.has(st)) continue;
        return true;
    }
    return false;
}


/**
 * Reactive binder for the Arsenal command buttons.
 *
 * Example:
 *     new ArsenalBinder({
 *         state: hub.state,
 *         client: hub.client,
 *         buttons: document.querySelectorAll('.ops-arsenal__btn[data-command]'),
 *     });
 */
export class ArsenalBinder {
    /**
     * @param {object} opts
     * @param {import('./appstate.js').AppState} opts.state
     * @param {import('./websocket-client.js').WebSocketClient} [opts.client]
     *        Optional — if omitted, clicks are no-ops (used for UI-only previews).
     * @param {NodeListOf<HTMLElement>|HTMLElement[]} opts.buttons
     * @param {number} [opts.inflightTimeoutMs=INFLIGHT_TIMEOUT_MS]
     */
    constructor({
        state,
        client = null,
        buttons,
        inflightTimeoutMs = INFLIGHT_TIMEOUT_MS,
    }) {
        if (!state || typeof state.subscribe !== 'function') {
            throw new TypeError('ArsenalBinder requires an AppState-like object');
        }
        this._state = state;
        this._client = client;
        this._buttons = Array.from(buttons ?? []).filter((btn) => btn && btn.dataset?.command);
        this._inflightTimeoutMs = inflightTimeoutMs;

        /** @private Map<command, {timerId: number|null, originallyDisabled: boolean}> */
        this._inflight = new Map();
        /** @private Map<HTMLElement, Function> */
        this._clickHandlers = new Map();
        /** @private Set<string> set of commands authored by the user (not backend-initiated). */
        this._userInitiated = new Set();

        this._bindClicks();
        this._unsubProcesses = this._state.subscribe('processes', (procs) => this._onProcesses(procs));
        this._unsubConflicts = this._state.subscribe('conflicts', (list) => this._onConflicts(list));

        // Initial pass using current state snapshots.
        this._onProcesses(this._state.processes);
        this._onConflicts(this._state.conflicts);
    }

    /** Clean up listeners and inflight timers. */
    dispose() {
        if (this._unsubProcesses) this._unsubProcesses();
        if (this._unsubConflicts) this._unsubConflicts();
        for (const [btn, handler] of this._clickHandlers) {
            btn.removeEventListener('click', handler);
        }
        this._clickHandlers.clear();
        for (const entry of this._inflight.values()) {
            if (entry && entry.timerId !== null) {
                clearTimeout(entry.timerId);
            }
        }
        this._inflight.clear();
    }

    // ──────────────────────────── Internals ────────────────────────────────

    /** @private */
    _bindClicks() {
        for (const btn of this._buttons) {
            const handler = () => this._onClick(btn);
            btn.addEventListener('click', handler);
            this._clickHandlers.set(btn, handler);
        }
    }

    /** @private */
    _onClick(btn) {
        const command = btn.dataset?.command;
        if (!command) return;
        if (btn.disabled) return;               // belt & suspenders
        if (this._inflight.has(command)) return;

        // Mark inflight locally before round-tripping the network so rapid
        // double-clicks resolve to a single dispatched frame.
        this._userInitiated.add(command);
        this._acquireInflight(command);
        this._updateButtonsFromState();

        if (this._client && typeof this._client.send === 'function') {
            const ok = this._client.send({
                action: 'command',
                command,
                ts: Date.now(),
            });
            if (!ok) {
                // Send failed → release immediately so the user can retry.
                this._releaseInflight(command);
                this._userInitiated.delete(command);
                this._updateButtonsFromState();
            }
        }
    }

    /** @private */
    _acquireInflight(command) {
        if (this._inflight.has(command)) return;
        const timerId = setTimeout(() => {
            this._releaseInflight(command);
            this._userInitiated.delete(command);
            this._updateButtonsFromState();
        }, this._inflightTimeoutMs);
        this._inflight.set(command, { timerId });
    }

    /** @private */
    _releaseInflight(command) {
        const entry = this._inflight.get(command);
        if (!entry) return;
        if (entry.timerId !== null) clearTimeout(entry.timerId);
        this._inflight.delete(command);
    }

    /** @private */
    _onProcesses(processes) {
        // If the backend now reports a terminal state for a user-initiated
        // command, release the inflight lock so the button becomes clickable
        // before the safety timeout fires.
        for (const command of Array.from(this._userInitiated)) {
            if (!isCommandActive(processes, command)) {
                this._releaseInflight(command);
                this._userInitiated.delete(command);
            }
        }
        this._updateButtonsFromState(processes, this._state.conflicts);
    }

    /** @private */
    _onConflicts(conflicts) {
        this._updateButtonsFromState(this._state.processes, conflicts);
    }

    /** @private */
    _updateButtonsFromState(processesOpt, conflictsOpt) {
        const processes = processesOpt ?? this._state.processes;
        const conflicts = conflictsOpt ?? this._state.conflicts;
        const anyActive = hasActiveProcess(processes);
        const hasConflict = Array.isArray(conflicts) ? conflicts.length > 0 : false;

        for (const btn of this._buttons) {
            const command = btn.dataset.command;
            const locked = this._inflight.has(command) || isCommandActive(processes, command);
            let disabled = locked;
            if (command === 'approve_conflict' && !hasConflict) disabled = true;
            if (command === 'cancel_process'   && !anyActive)   disabled = true;

            this._setDisabled(btn, disabled);
            btn.setAttribute('aria-busy', locked ? 'true' : 'false');
        }
    }

    /** @private */
    _setDisabled(btn, disabled) {
        btn.disabled = Boolean(disabled);
        if (disabled) {
            btn.setAttribute('aria-disabled', 'true');
        } else {
            btn.removeAttribute('aria-disabled');
        }
    }
}

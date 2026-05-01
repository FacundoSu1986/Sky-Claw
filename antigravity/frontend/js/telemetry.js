/* ═══════════════════════════════════════════════════════════════════════════════════
   SKY-CLAW — OPERATIONS HUB · Telemetry binder
   Reactive binder for the right-panel Telemetría cards (CPU, Memoria,
   Procesos, Uptime).

   Directive (Fase 5):
     • DOM writes MUST coalesce at the browser's display refresh rate
       (≤60Hz). We use the same `_renderPending` + requestAnimationFrame
       pattern as LogView: only one frame is ever queued, no matter how many
       telemetry frames arrive in a single tick. That keeps a 200Hz producer
       from thrashing the layout engine.
     • Uptime displays as HH:MM:SS even when the backend is silent — a
       local ticker increments the last-known `uptimeMs` every second so the
       panel never appears frozen.
     • Pure helpers (formatUptime, formatCpu, formatMemory, clampPct) are
       exported for unit testing without a DOM.
   ═══════════════════════════════════════════════════════════════════════════════════ */

/** Local tick cadence for the Uptime display (ms). */
export const UPTIME_TICK_MS = 1000;


/** Clamp a percentage value into the [0, 100] range. */
export function clampPct(value) {
    if (!Number.isFinite(value)) return 0;
    if (value < 0) return 0;
    if (value > 100) return 100;
    return value;
}


/**
 * Format a CPU percentage as e.g. "42.7". Accepts 0–100 (%) or 0–1 (ratio).
 * Ratios are auto-scaled to percents.
 *
 * @param {number} value
 * @returns {string}
 */
export function formatCpu(value) {
    if (!Number.isFinite(value)) return '0.0';
    const pct = value > 1.5 ? value : value * 100;
    return clampPct(pct).toFixed(1);
}


/**
 * Format bytes-or-MB memory into a compact label.
 * Inputs > 10_000 are assumed to be bytes; otherwise MB.
 *
 * @param {number} value
 * @returns {string}
 */
export function formatMemory(value) {
    if (!Number.isFinite(value) || value <= 0) return '0';
    const mb = value > 10000 ? (value / (1024 * 1024)) : value;
    if (mb >= 1024) return (mb / 1024).toFixed(1);  // GB-scaled
    return Math.round(mb).toString();
}


/** Return the corresponding memory unit label ("MB" or "GB") for the same input. */
export function formatMemoryUnit(value) {
    if (!Number.isFinite(value) || value <= 0) return 'MB';
    const mb = value > 10000 ? (value / (1024 * 1024)) : value;
    return mb >= 1024 ? 'GB' : 'MB';
}


/**
 * Format an uptime millisecond count as HH:MM:SS (days wrap into hours).
 *
 * @param {number} ms
 * @returns {string}
 */
export function formatUptime(ms) {
    if (!Number.isFinite(ms) || ms <= 0) return '00:00:00';
    const totalSec = Math.floor(ms / 1000);
    const h = Math.floor(totalSec / 3600);
    const m = Math.floor((totalSec % 3600) / 60);
    const s = totalSec % 60;
    return `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
}


/**
 * Reactive binder for the Telemetría cards.
 *
 * Example:
 *     new TelemetryBinder({
 *         state: hub.state,
 *         elements: {
 *             cpuValue: document.getElementById('ops-cpu'),
 *             cpuBar:   document.getElementById('ops-cpu-bar'),
 *             memValue: document.getElementById('ops-mem'),
 *             memUnit:  document.querySelector('...'),
 *             memBar:   document.getElementById('ops-mem-bar'),
 *             procs:    document.getElementById('ops-procs'),
 *             uptime:   document.getElementById('ops-uptime'),
 *         },
 *     });
 */
export class TelemetryBinder {
    /**
     * @param {object} opts
     * @param {import('./appstate.js').AppState} opts.state
     * @param {object} opts.elements
     * @param {HTMLElement} [opts.elements.cpuValue]
     * @param {HTMLElement} [opts.elements.cpuBar]
     * @param {HTMLElement} [opts.elements.memValue]
     * @param {HTMLElement} [opts.elements.memUnit]
     * @param {HTMLElement} [opts.elements.memBar]
     * @param {HTMLElement} [opts.elements.procs]
     * @param {HTMLElement} [opts.elements.uptime]
     * @param {number}      [opts.uptimeTickMs=UPTIME_TICK_MS]
     */
    constructor({ state, elements = {}, uptimeTickMs = UPTIME_TICK_MS }) {
        if (!state || typeof state.subscribe !== 'function') {
            throw new TypeError('TelemetryBinder requires an AppState-like object');
        }
        this._state = state;
        this._el = elements;
        this._uptimeTickMs = uptimeTickMs;

        /** @private */ this._renderPending = false;
        /** @private */ this._pendingFrameHandle = null;
        /** @private */ this._uptimeTimer = null;
        /** @private */ this._lastTelemetryReceivedAt = Date.now();

        this._unsubTelemetry = this._state.subscribe('telemetry', () => this._scheduleRender());
        this._unsubProcesses = this._state.subscribe('processes', () => this._scheduleRender());

        // Initial paint from the current snapshot.
        this._scheduleRender();
        this._startUptimeTicker();
    }

    /** Stop timers, unsubscribe from state. */
    dispose() {
        if (this._unsubTelemetry) this._unsubTelemetry();
        if (this._unsubProcesses) this._unsubProcesses();
        this._stopUptimeTicker();
        if (this._pendingFrameHandle !== null && typeof cancelAnimationFrame === 'function') {
            cancelAnimationFrame(this._pendingFrameHandle);
        }
        this._pendingFrameHandle = null;
        this._renderPending = false;
    }

    // ──────────────────────────── Internals ────────────────────────────────

    /** @private */
    _scheduleRender() {
        // Coalesce bursts: only one frame in-flight at a time. The boolean is
        // the source of truth so a synchronous raf (tests, old browsers)
        // never double-dispatches.
        if (this._renderPending) return;
        this._renderPending = true;
        const raf = (typeof requestAnimationFrame === 'function')
            ? requestAnimationFrame
            : (fn) => setTimeout(fn, 16);
        const handle = raf(() => {
            this._renderPending = false;
            this._pendingFrameHandle = null;
            this._render();
        });
        if (this._renderPending) {
            this._pendingFrameHandle = handle;
        }
    }

    /** @private */
    _render() {
        const t = this._state.telemetry ?? {};
        this._lastTelemetryReceivedAt = Date.now();

        if (this._el.cpuValue) this._el.cpuValue.textContent = formatCpu(t.cpu);
        if (this._el.cpuBar)   this._setBarWidth(this._el.cpuBar, formatCpu(t.cpu));

        if (this._el.memValue) this._el.memValue.textContent = formatMemory(t.memory);
        if (this._el.memUnit)  this._el.memUnit.textContent = formatMemoryUnit(t.memory);
        if (this._el.memBar && Number.isFinite(t.memoryMax) && t.memoryMax > 0) {
            const pct = clampPct((t.memory / t.memoryMax) * 100);
            this._setBarWidth(this._el.memBar, pct.toFixed(1));
        }

        if (this._el.procs) {
            // Prefer the live process Map size when populated; fall back to the
            // telemetry-reported counter so a missing sidecar doesn't blank the UI.
            const fromMap = this._state.processes?.size ?? 0;
            const fromTel = Number.isFinite(t.activeProcesses) ? t.activeProcesses : 0;
            const active = Math.max(fromMap, fromTel);
            this._el.procs.textContent = String(active);
        }

        if (this._el.uptime) {
            this._el.uptime.textContent = formatUptime(t.uptimeMs);
        }
    }

    /** @private */
    _setBarWidth(el, pctStr) {
        if (!el) return;
        const n = Number.parseFloat(pctStr);
        el.style.width = `${clampPct(n).toFixed(1)}%`;
    }

    /** @private */
    _startUptimeTicker() {
        this._stopUptimeTicker();
        if (!this._el.uptime || this._uptimeTickMs <= 0) return;
        this._uptimeTimer = setInterval(() => {
            const t = this._state.telemetry ?? {};
            const base = Number.isFinite(t.uptimeMs) ? t.uptimeMs : 0;
            if (base <= 0) return;   // never extrapolate when backend is silent
            const since = Date.now() - this._lastTelemetryReceivedAt;
            this._el.uptime.textContent = formatUptime(base + since);
        }, this._uptimeTickMs);
    }

    /** @private */
    _stopUptimeTicker() {
        if (this._uptimeTimer !== null) {
            clearInterval(this._uptimeTimer);
            this._uptimeTimer = null;
        }
    }
}

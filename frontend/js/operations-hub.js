/* ═══════════════════════════════════════════════════════════════════════════════════
   SKY-CLAW — OPERATIONS HUB · bootstrap + WS→AppState wiring
   This module is the glue between `websocket-client.js` and `appstate.js`.
   It owns no DOM: Phase 4 (Orbe de Visión virtual scroll) and Phase 5
   (Arsenal + Telemetría reactive binders) subscribe to the exported
   AppState instance to render.

   Topic routing (CoreEventBus → AppState key) is governed by the
   `ROUTES` table below, which mirrors the forwarded patterns declared in
   `sky_claw/web/operations_hub_ws.py::DEFAULT_FORWARDED_PATTERNS`.
   ═══════════════════════════════════════════════════════════════════════════════════ */

import { AppState } from './appstate.js';
import { WebSocketClient, buildWsUrl } from './websocket-client.js';
import { LogView } from './log-view.js';
import { ArsenalBinder } from './arsenal.js';
import { TelemetryBinder } from './telemetry.js';
import { PanelCollapseBinder } from './panel-collapse.js';
import { StatusBarBinder } from './status-bar.js';

/** WebSocket endpoint served by OperationsHubWSHandler. */
export const WS_PATH = '/api/status';


// ───────────────────────────── Topic routing ────────────────────────────────

/**
 * Match a topic string against a simple wildcard pattern (only the trailing
 * `.*` is supported, matching how the backend's fnmatch filters are used).
 *
 * @param {string} topic e.g. 'ops.log.info'
 * @param {string} pattern e.g. 'ops.log.*'
 * @returns {boolean}
 */
export function matchesTopic(topic, pattern) {
    if (!topic || !pattern) return false;
    if (pattern === topic) return true;
    if (pattern.endsWith('.*')) {
        const prefix = pattern.slice(0, -1); // drop the '*', keep the trailing dot
        return topic.startsWith(prefix);
    }
    return false;
}

/**
 * Map a log-ish topic to a level understood by AppState / CSS filters.
 * Falls back to 'info' when the payload does not disclose a level.
 */
function inferLogLevel(topic, payload) {
    const raw = String(payload?.level ?? '').toLowerCase();
    if (['debug', 'info', 'success', 'warning', 'warn', 'error', 'err'].includes(raw)) {
        if (raw === 'warn') return 'warning';
        if (raw === 'err') return 'error';
        return raw;
    }
    // Backend convention: topic suffix encodes severity (ops.log.warning etc).
    const suffix = topic.split('.').pop();
    if (['debug', 'info', 'success', 'warning', 'error'].includes(suffix)) return suffix;
    if (suffix === 'warn') return 'warning';
    if (suffix === 'err') return 'error';
    return 'info';
}

/** Extract a renderable message line from arbitrary payload shapes. */
function extractMessage(payload) {
    if (payload == null) return '';
    if (typeof payload === 'string') return payload;
    if (typeof payload.message === 'string') return payload.message;
    if (typeof payload.msg === 'string') return payload.msg;
    if (typeof payload.text === 'string') return payload.text;
    try {
        return JSON.stringify(payload);
    } catch {
        return String(payload);
    }
}


/**
 * Apply a single decoded server frame to the given AppState.
 * Exported so unit tests can exercise the routing without a live socket.
 *
 * @param {AppState} state
 * @param {{event_type: string, payload?: any, timestamp_ms?: number, source?: string}} frame
 */
export function applyFrameToState(state, frame) {
    if (!frame || typeof frame !== 'object') return;
    const topic = String(frame.event_type ?? '');
    const payload = frame.payload ?? {};

    // Meta frames handled before the routing table.
    if (topic === 'snapshot') {
        // Server-side snapshot signal: mark connection as live. Any seeded data
        // is expected to arrive in subsequent per-topic frames.
        state.setConnection('connected', { attempts: 0 });
        return;
    }
    if (topic === 'pong') {
        // Heartbeat echo — no state change.
        return;
    }

    // Logs: every forwarded topic produces a log line so the Orbe de Visión
    // acts as a running ticker, regardless of which subsystem emitted it.
    if (
        matchesTopic(topic, 'ops.log.*')
        || matchesTopic(topic, 'ops.process.*')
        || matchesTopic(topic, 'synthesis.pipeline.*')
        || matchesTopic(topic, 'xedit.patch.*')
        || matchesTopic(topic, 'pipeline.dyndolod.*')
        || matchesTopic(topic, 'system.modlist.*')
        || matchesTopic(topic, 'ops.tool.*')
    ) {
        state.appendLog({
            level: inferLogLevel(topic, payload),
            message: extractMessage(payload),
            source: frame.source ?? null,
            timestampMs: Number.isFinite(frame.timestamp_ms) ? frame.timestamp_ms : Date.now(),
            topic,
            meta: typeof payload === 'object' ? payload : null,
        });
    }

    // Process table
    if (matchesTopic(topic, 'ops.process.*')) {
        const id = payload?.process_id ?? payload?.id ?? payload?.name;
        if (id) {
            state.updateProcess(String(id), {
                state: payload.state ?? payload.status ?? 'unknown',
                label: payload.label ?? payload.name ?? String(id),
                progress: Number.isFinite(payload.progress) ? payload.progress : null,
                topic,
                updatedAt: frame.timestamp_ms ?? Date.now(),
            });
        }
    }

    // FASE 1.5.4: Granular tool lifecycle events — map to process table
    // so the UI shows real spinners/progress instead of optimistic success.
    const TOOL_EVENT_MAP = {
        'tool_started':            'running',
        'tool_progress':           'running',
        'tool_completed':          'completed',
        'tool_failed':             'failed',
        'tool_requires_approval':  'awaiting_approval',
    };
    const toolState = TOOL_EVENT_MAP[topic];
    if (toolState) {
        const toolId = payload?.task_id ?? payload?.tool ?? topic;
        const toolLabel = payload?.tool
            ? `🔧 ${payload.tool}`
            : topic;
        state.updateProcess(String(toolId), {
            state: toolState,
            label: toolLabel,
            progress: Number.isFinite(payload?.progress) ? payload.progress : null,
            topic,
            updatedAt: frame.timestamp_ms ?? Date.now(),
        });
    }

    // Telemetry
    if (
        matchesTopic(topic, 'ops.telemetry.*')
        || matchesTopic(topic, 'system.telemetry.*')
    ) {
        const patch = {};
        if (Number.isFinite(payload.cpu)) patch.cpu = payload.cpu;
        if (Number.isFinite(payload.cpu_percent)) patch.cpu = payload.cpu_percent;
        if (Number.isFinite(payload.memory)) patch.memory = payload.memory;
        if (Number.isFinite(payload.memory_mb)) patch.memory = payload.memory_mb;
        if (Number.isFinite(payload.memory_max)) patch.memoryMax = payload.memory_max;
        if (Number.isFinite(payload.active_processes)) patch.activeProcesses = payload.active_processes;
        if (Number.isFinite(payload.uptime_ms)) patch.uptimeMs = payload.uptime_ms;
        if (Number.isFinite(payload.uptime_s)) patch.uptimeMs = payload.uptime_s * 1000;
        if (Object.keys(patch).length > 0) {
            state.updateTelemetry(patch);
        }
    }

    // HITL / conflict queue
    if (matchesTopic(topic, 'ops.hitl.*') || matchesTopic(topic, 'ops.conflict.*')) {
        const id = payload?.id ?? payload?.conflict_id;
        if (id) {
            if (topic.endsWith('.resolved') || topic.endsWith('.dismissed')) {
                state.resolveConflict(String(id));
            } else {
                state.addConflict({
                    id: String(id),
                    title: payload.title ?? payload.label ?? 'Conflicto sin título',
                    severity: payload.severity ?? 'warning',
                    detail: payload.detail ?? payload.description ?? null,
                    topic,
                    createdAt: frame.timestamp_ms ?? Date.now(),
                });
            }
        }
    }
}


// ───────────────────────────── Bootstrap ────────────────────────────────────

/**
 * Build and wire a (state, client) pair. Defensively dependency-injectable so
 * unit tests can feed a fake WebSocketClient and an isolated AppState.
 *
 * @param {object} [options]
 * @param {string} [options.url]         Defaults to buildWsUrl(WS_PATH).
 * @param {AppState} [options.state]     Injectable for tests.
 * @param {WebSocketClient} [options.client] Injectable for tests.
 * @param {boolean} [options.autoConnect=true]
 * @returns {{state: AppState, client: WebSocketClient, dispose: () => void}}
 */
export function createOperationsHub({
    url,
    state = new AppState(),
    client,
    autoConnect = true,
} = {}) {
    const wsUrl = url ?? (typeof window !== 'undefined' ? buildWsUrl(WS_PATH) : null);
    const wsClient = client ?? new WebSocketClient(wsUrl);

    const unsubs = [
        wsClient.on('connecting', ({ attempts }) => {
            state.setConnection('connecting', { attempts });
        }),
        wsClient.on('open', ({ attempts }) => {
            state.setConnection('connected', { attempts });
        }),
        wsClient.on('close', ({ code, reason }) => {
            state.setConnection('disconnected', { lastError: reason || `close ${code ?? '?'}` });
        }),
        wsClient.on('reconnecting', ({ attempts, delayMs }) => {
            state.setConnection('reconnecting', { attempts, lastError: `retrying in ${Math.round(delayMs)}ms` });
        }),
        wsClient.on('error', ({ error, event }) => {
            // eslint-disable-next-line no-console
            console.warn('[OperationsHub] WS error:', error ?? event);
        }),
        wsClient.on('frame', (frame) => {
            applyFrameToState(state, frame);
        }),
    ];

    if (autoConnect) {
        wsClient.connect();
    }

    const dispose = () => {
        for (const off of unsubs) {
            try { off(); } catch { /* no-op */ }
        }
        wsClient.disconnect();
    };

    return { state, client: wsClient, dispose };
}


/**
 * Mount the DOM binders that are ready to bind against the given state.
 * Phase 4 introduced LogView; Phase 5 adds Arsenal, Telemetría, the panel
 * collapse toggle, and the status-bar reactive binder.
 *
 * Exported so tests and future reuse can opt in/out granularly.
 *
 * @param {AppState} state
 * @param {WebSocketClient} [client] Optional — Arsenal needs it to send commands.
 * @returns {{logView: LogView|null, arsenal: ArsenalBinder|null, telemetry: TelemetryBinder|null, panelCollapse: PanelCollapseBinder|null, statusBar: StatusBarBinder|null, dispose: () => void}}
 */
export function mountDomBinders(state, client = null) {
    const binders = [];

    // ── Orbe de Visión (logs) ───────────────────────────────────────────────
    const logContainer = document.getElementById('ops-log-stream');
    const filterButtons = document.querySelectorAll('.ops-orbe__filter-btn');
    const clearButton = document.getElementById('ops-clear-logs');
    const autoScrollButton = document.getElementById('ops-autoscroll');
    const emptyState = document.getElementById('ops-orbe-empty');

    let logView = null;
    if (logContainer) {
        logView = new LogView({
            state,
            container: logContainer,
            filterButtons,
            clearButton,
            autoScrollButton,
            emptyState,
        });
        binders.push(logView);
    } else {
        // eslint-disable-next-line no-console
        console.warn('[OperationsHub] #ops-log-stream not found; LogView not mounted');
    }

    // ── Arsenal (command buttons) ───────────────────────────────────────────
    const arsenalButtons = document.querySelectorAll('.ops-arsenal__btn[data-command]');
    let arsenal = null;
    if (arsenalButtons && arsenalButtons.length > 0) {
        arsenal = new ArsenalBinder({ state, client, buttons: arsenalButtons });
        binders.push(arsenal);
    }

    // ── Telemetría (CPU / RAM / procs / uptime) ─────────────────────────────
    let telemetry = null;
    const telemetryElements = {
        cpuValue: document.getElementById('ops-cpu'),
        cpuBar:   document.getElementById('ops-cpu-bar'),
        memValue: document.getElementById('ops-mem'),
        memUnit:  document.getElementById('ops-mem-unit'),
        memBar:   document.getElementById('ops-mem-bar'),
        procs:    document.getElementById('ops-procs'),
        uptime:   document.getElementById('ops-uptime'),
    };
    if (Object.values(telemetryElements).some(Boolean)) {
        telemetry = new TelemetryBinder({ state, elements: telemetryElements });
        binders.push(telemetry);
    }

    // ── Panel collapse (Ctrl+B) ─────────────────────────────────────────────
    const root = document.getElementById('ops-hub');
    const collapseToggle = document.getElementById('ops-collapse-toggle');
    const panelsScroller = document.getElementById('ops-panels-scroller');
    let panelCollapse = null;
    if (root && collapseToggle) {
        panelCollapse = new PanelCollapseBinder({
            root,
            toggleButton: collapseToggle,
            scroller: panelsScroller,
        });
        binders.push(panelCollapse);
    }

    // ── Status bar (connection + HITL) ──────────────────────────────────────
    const indicator = document.getElementById('ops-conn-indicator');
    const statusBarElements = {
        dot: indicator?.querySelector?.('.ops-statusbar__dot') ?? null,
        indicator: indicator ?? null,
        connText: document.getElementById('ops-conn-text'),
        hitlText: document.getElementById('ops-hitl-text'),
    };
    let statusBar = null;
    if (statusBarElements.connText || statusBarElements.hitlText) {
        statusBar = new StatusBarBinder({ state, elements: statusBarElements });
        binders.push(statusBar);
    }

    const dispose = () => {
        for (const b of binders) {
            try { b.dispose?.(); } catch { /* no-op */ }
        }
    };

    return { logView, arsenal, telemetry, panelCollapse, statusBar, dispose };
}


// ─────────────────── Auto-bootstrap when loaded in a browser ───────────────

if (typeof window !== 'undefined' && typeof document !== 'undefined') {
    const boot = () => {
        const hub = createOperationsHub();
        const binders = mountDomBinders(hub.state, hub.client);
        // Expose on window for binders + DevTools debugging.
        window.SkyClawOperationsHub = { ...hub, binders };
    };
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', boot, { once: true });
    } else {
        boot();
    }
}

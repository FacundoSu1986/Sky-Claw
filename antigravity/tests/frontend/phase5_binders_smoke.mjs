/* Smoke test for Phase 5 binders:
 *   - ArsenalBinder         (frontend/js/arsenal.js)
 *   - TelemetryBinder       (frontend/js/telemetry.js)
 *   - PanelCollapseBinder   (frontend/js/panel-collapse.js)
 *   - StatusBarBinder       (frontend/js/status-bar.js)
 *
 * Uses a hand-rolled DOM stub (classList + focus + contains + document-level
 * keydown) to avoid jsdom while still covering integration paths end-to-end.
 *
 * Exits non-zero on any failure.
 * Usage:   node tests/frontend/phase5_binders_smoke.mjs
 */

import { strict as assert } from 'node:assert';

import {
    ArsenalBinder,
    hasActiveProcess,
    isCommandActive,
    ACTIVE_PROCESS_STATES,
    TERMINAL_PROCESS_STATES,
} from '../../frontend/js/arsenal.js';
import {
    TelemetryBinder,
    clampPct,
    formatCpu,
    formatMemory,
    formatMemoryUnit,
    formatUptime,
} from '../../frontend/js/telemetry.js';
import {
    PanelCollapseBinder,
    isCollapseShortcut,
} from '../../frontend/js/panel-collapse.js';
import {
    StatusBarBinder,
    connectionLabel,
    connectionDotClass,
    hitlLabel,
    DOT_CLASSES,
} from '../../frontend/js/status-bar.js';
import { AppState } from '../../frontend/js/appstate.js';

let passed = 0;
function test(name, fn) {
    try {
        fn();
        passed += 1;
        // eslint-disable-next-line no-console
        console.log(`  OK  ${name}`);
    } catch (err) {
        // eslint-disable-next-line no-console
        console.error(`  FAIL  ${name}\n    ${err.stack ?? err.message}`);
        process.exitCode = 1;
    }
}

// ─── DOM stub ──────────────────────────────────────────────────────────────

class StubTokenList {
    constructor() { this._set = new Set(); }
    add(...xs)    { for (const x of xs) this._set.add(x); }
    remove(...xs) { for (const x of xs) this._set.delete(x); }
    contains(x)   { return this._set.has(x); }
    toggle(x, force) {
        const want = (force === undefined) ? !this.contains(x) : Boolean(force);
        if (want) this.add(x); else this.remove(x);
        return want;
    }
    get size() { return this._set.size; }
    values()   { return this._set.values(); }
    toString() { return [...this._set].join(' '); }
}

class StubElement {
    constructor(tag = 'div') {
        this.tagName = tag.toUpperCase();
        this.children = [];
        this.parent = null;
        this.style = {};
        this.dataset = {};
        this.attributes = Object.create(null);
        this.classList = new StubTokenList();
        this.className = '';
        this.textContent = '';
        this.title = '';
        this.listeners = Object.create(null);
        this.disabled = false;
        this.isContentEditable = false;
        this._focused = false;
    }
    appendChild(child) { this.children.push(child); child.parent = this; return child; }
    append(...kids)    { for (const k of kids) this.appendChild(k); }
    replaceChildren(...kids) {
        this.children = [];
        for (const k of kids) {
            if (k && Array.isArray(k.children)) {
                for (const c of k.children) this.appendChild(c);
            } else if (k != null) this.appendChild(k);
        }
    }
    querySelector(sel) {
        // Only supports class selector ".foo"
        if (typeof sel === 'string' && sel.startsWith('.')) {
            const want = sel.slice(1);
            const stack = [...this.children];
            while (stack.length) {
                const n = stack.pop();
                if (n.classList && n.classList.contains(want)) return n;
                if (typeof n.className === 'string' && n.className.split(/\s+/).includes(want)) return n;
                if (n.children) stack.push(...n.children);
            }
        }
        return null;
    }
    contains(target) {
        if (target === this) return true;
        for (const c of this.children) {
            if (c === target) return true;
            if (typeof c.contains === 'function' && c.contains(target)) return true;
        }
        return false;
    }
    setAttribute(name, value) { this.attributes[name] = String(value); }
    getAttribute(name)        { return name in this.attributes ? this.attributes[name] : null; }
    removeAttribute(name)     { delete this.attributes[name]; }
    addEventListener(event, fn)    { (this.listeners[event] ??= []).push(fn); }
    removeEventListener(event, fn) {
        const arr = this.listeners[event];
        if (!arr) return;
        const i = arr.indexOf(fn);
        if (i >= 0) arr.splice(i, 1);
    }
    dispatchEvent(event) {
        const arr = this.listeners[event.type] ?? [];
        for (const fn of arr.slice()) fn(event);
    }
    click() { this.dispatchEvent({ type: 'click', target: this }); }
    focus() { this._focused = true; stubDoc.activeElement = this; }
}

class StubDocument {
    constructor() {
        this._byId = new Map();
        this.listeners = Object.create(null);
        this.activeElement = null;
    }
    createElement(tag) { return new StubElement(tag); }
    createDocumentFragment() { return new StubElement('#fragment'); }
    getElementById(id) { return this._byId.get(id) ?? null; }
    addEventListener(event, fn)    { (this.listeners[event] ??= []).push(fn); }
    removeEventListener(event, fn) {
        const arr = this.listeners[event];
        if (!arr) return;
        const i = arr.indexOf(fn);
        if (i >= 0) arr.splice(i, 1);
    }
    dispatchEvent(event) {
        const arr = this.listeners[event.type] ?? [];
        for (const fn of arr.slice()) fn(event);
    }
    register(id, el) { this._byId.set(id, el); return el; }
}

const stubDoc = new StubDocument();
globalThis.document = stubDoc;
globalThis.requestAnimationFrame = (fn) => { fn(); return 1; };
globalThis.cancelAnimationFrame  = () => {};


// ─── Pure helpers ──────────────────────────────────────────────────────────

test('arsenal: ACTIVE / TERMINAL sets are disjoint', () => {
    for (const s of ACTIVE_PROCESS_STATES) {
        assert.equal(TERMINAL_PROCESS_STATES.has(s), false, `"${s}" in both sets`);
    }
});

test('arsenal: hasActiveProcess detects running state', () => {
    const map = new Map([
        ['p1', { state: 'running' }],
        ['p2', { state: 'finished' }],
    ]);
    assert.equal(hasActiveProcess(map), true);
});

test('arsenal: hasActiveProcess ignores all-terminal map', () => {
    const map = new Map([
        ['p1', { state: 'finished' }],
        ['p2', { state: 'failed' }],
    ]);
    assert.equal(hasActiveProcess(map), false);
});

test('arsenal: isCommandActive matches by command field', () => {
    const map = new Map([
        ['a', { command: 'run_full_scan', state: 'running' }],
        ['b', { command: 'launch_mo2',    state: 'running' }],
    ]);
    assert.equal(isCommandActive(map, 'run_full_scan'), true);
    assert.equal(isCommandActive(map, 'generate_dyndolod'), false);
});

test('arsenal: isCommandActive ignores terminal match', () => {
    const map = new Map([
        ['a', { command: 'run_full_scan', state: 'finished' }],
    ]);
    assert.equal(isCommandActive(map, 'run_full_scan'), false);
});

test('telemetry: clampPct bounds [0,100]', () => {
    assert.equal(clampPct(-5), 0);
    assert.equal(clampPct(150), 100);
    assert.equal(clampPct(42.5), 42.5);
    assert.equal(clampPct(Number.NaN), 0);
});

test('telemetry: formatCpu handles percent and ratio', () => {
    assert.equal(formatCpu(0), '0.0');
    assert.equal(formatCpu(42.7), '42.7');
    assert.equal(formatCpu(0.73), '73.0');     // ratio auto-scaled
    assert.equal(formatCpu(110), '100.0');     // clamped
});

test('telemetry: formatMemory scales MB vs bytes vs GB', () => {
    assert.equal(formatMemory(0), '0');
    assert.equal(formatMemory(512), '512');           // 512 MB → MB
    assert.equal(formatMemoryUnit(512), 'MB');
    assert.equal(formatMemory(1500), '1.5');          // 1500 MB auto-scales → GB
    assert.equal(formatMemoryUnit(1500), 'GB');
    assert.equal(formatMemory(2048), '2.0');          // 2 GB
    assert.equal(formatMemoryUnit(2048), 'GB');
    // A value > 10_000 is treated as bytes and converted to MB first,
    // then possibly to GB. 5 MB → '5'.
    assert.equal(formatMemory(5_242_880), '5');
});

test('telemetry: formatUptime formats HH:MM:SS', () => {
    assert.equal(formatUptime(0), '00:00:00');
    assert.equal(formatUptime(1000), '00:00:01');
    assert.equal(formatUptime(3_600_000 + 120_000 + 7_000), '01:02:07');
    assert.equal(formatUptime(25 * 3_600_000), '25:00:00');  // past 24h, no wrap
});

test('panel-collapse: isCollapseShortcut detects Ctrl+B and Cmd+B', () => {
    assert.equal(isCollapseShortcut({ key: 'b', ctrlKey: true }),  true);
    assert.equal(isCollapseShortcut({ key: 'B', metaKey: true }),  true);
    assert.equal(isCollapseShortcut({ key: 'b', ctrlKey: false }), false);
    assert.equal(isCollapseShortcut({ key: 'n', ctrlKey: true }),  false);
    assert.equal(isCollapseShortcut({ key: 'b', ctrlKey: true, shiftKey: true }), false);
});

test('status-bar: connectionLabel in Spanish', () => {
    assert.equal(connectionLabel({ status: 'connected' }),   'Conectado');
    assert.equal(connectionLabel({ status: 'connecting' }),  'Conectando…');
    assert.equal(connectionLabel({ status: 'reconnecting', attempts: 3 }), 'Reconectando (intento 3)');
    assert.equal(connectionLabel({ status: 'reconnecting' }), 'Reconectando…');
    assert.equal(connectionLabel({ status: 'disconnected' }), 'Desconectado');
    assert.equal(connectionLabel({ status: 'closed' }),       'Cerrado');
    assert.equal(connectionLabel(null),                       'Desconectado');
});

test('status-bar: connectionDotClass maps to CSS modifiers', () => {
    assert.equal(connectionDotClass({ status: 'connected' }),   'ops-statusbar__dot--ok');
    assert.equal(connectionDotClass({ status: 'connecting' }),  'ops-statusbar__dot--warn');
    assert.equal(connectionDotClass({ status: 'reconnecting' }), 'ops-statusbar__dot--warn');
    assert.equal(connectionDotClass({ status: 'disconnected' }), 'ops-statusbar__dot--err');
    assert.equal(connectionDotClass({ status: 'closed' }),       'ops-statusbar__dot--err');
});

test('status-bar: hitlLabel pluralisation', () => {
    assert.equal(hitlLabel(0), '0 alertas HITL');
    assert.equal(hitlLabel(1), '1 alerta HITL');
    assert.equal(hitlLabel(5), '5 alertas HITL');
});

// ─── ArsenalBinder integration ─────────────────────────────────────────────

function makeArsenalFixture() {
    const mk = (cmd) => {
        const b = new StubElement('button');
        b.dataset.command = cmd;
        return b;
    };
    return {
        runFull:  mk('run_full_scan'),
        mo2:      mk('launch_mo2'),
        dyn:      mk('generate_dyndolod'),
        approve:  mk('approve_conflict'),
        review:   mk('manual_review'),
        cancel:   mk('cancel_process'),
    };
}

test('ArsenalBinder: approve_conflict disabled when no conflicts', () => {
    const state = new AppState();
    const fx = makeArsenalFixture();
    const binder = new ArsenalBinder({
        state,
        buttons: Object.values(fx),
    });
    assert.equal(fx.approve.disabled, true);
    assert.equal(fx.cancel.disabled, true);
    // Regular commands remain clickable
    assert.equal(fx.runFull.disabled, false);
    binder.dispose();
});

test('ArsenalBinder: approve_conflict enables when state has a conflict', () => {
    const state = new AppState();
    const fx = makeArsenalFixture();
    const binder = new ArsenalBinder({
        state,
        buttons: Object.values(fx),
    });
    state.addConflict({ id: 'c1', title: 'Conflict 1' });
    assert.equal(fx.approve.disabled, false);
    binder.dispose();
});

test('ArsenalBinder: cancel_process enables when a process is running', () => {
    const state = new AppState();
    const fx = makeArsenalFixture();
    const binder = new ArsenalBinder({
        state,
        buttons: Object.values(fx),
    });
    state.updateProcess('p1', { state: 'running', command: 'manual_review' });
    assert.equal(fx.cancel.disabled, false);
    binder.dispose();
});

test('ArsenalBinder: click sends a command frame and locks inflight', () => {
    const state = new AppState();
    const fx = makeArsenalFixture();
    const sent = [];
    const fakeClient = { send: (obj) => { sent.push(obj); return true; } };
    const binder = new ArsenalBinder({
        state, client: fakeClient, buttons: Object.values(fx),
    });
    fx.runFull.click();
    assert.equal(sent.length, 1);
    assert.equal(sent[0].action, 'command');
    assert.equal(sent[0].command, 'run_full_scan');
    assert.equal(fx.runFull.disabled, true);
    assert.equal(fx.runFull.getAttribute('aria-busy'), 'true');
    // Second click while inflight is a no-op
    fx.runFull.click();
    assert.equal(sent.length, 1);
    binder.dispose();
});

test('ArsenalBinder: terminal process state releases inflight lock', () => {
    const state = new AppState();
    const fx = makeArsenalFixture();
    const fakeClient = { send: () => true };
    const binder = new ArsenalBinder({
        state, client: fakeClient, buttons: Object.values(fx),
    });
    fx.runFull.click();
    assert.equal(fx.runFull.disabled, true);
    // Backend reports running, then finished.
    state.updateProcess('run_full_scan', { command: 'run_full_scan', state: 'running' });
    assert.equal(fx.runFull.disabled, true);
    state.updateProcess('run_full_scan', { command: 'run_full_scan', state: 'finished' });
    assert.equal(fx.runFull.disabled, false);
    assert.equal(fx.runFull.getAttribute('aria-busy'), 'false');
    binder.dispose();
});

test('ArsenalBinder: send() failure releases inflight lock immediately', () => {
    const state = new AppState();
    const fx = makeArsenalFixture();
    const badClient = { send: () => false };
    const binder = new ArsenalBinder({
        state, client: badClient, buttons: Object.values(fx),
    });
    fx.runFull.click();
    assert.equal(fx.runFull.disabled, false);
    binder.dispose();
});

// ─── TelemetryBinder integration ───────────────────────────────────────────

function makeTelemetryFixture() {
    return {
        cpuValue: new StubElement('span'),
        cpuBar:   new StubElement('div'),
        memValue: new StubElement('span'),
        memUnit:  new StubElement('span'),
        memBar:   new StubElement('div'),
        procs:    new StubElement('span'),
        uptime:   new StubElement('span'),
    };
}

test('TelemetryBinder: updateTelemetry flushes to DOM via rAF', () => {
    const state = new AppState();
    const el = makeTelemetryFixture();
    const binder = new TelemetryBinder({ state, elements: el });
    // 800 MB / 3000 MB max — stays in MB range so the test also exercises
    // the unit-span binding.
    state.updateTelemetry({ cpu: 42, memory: 800, memoryMax: 3000, uptimeMs: 125_000 });
    assert.equal(el.cpuValue.textContent, '42.0');
    assert.equal(el.cpuBar.style.width, '42.0%');
    assert.equal(el.memValue.textContent, '800');
    assert.equal(el.memUnit.textContent, 'MB');
    // memory 800 / memoryMax 3000 → 26.67%
    assert.ok(/^26\.[67]\d*%$/.test(el.memBar.style.width), `memBar width unexpected: ${el.memBar.style.width}`);
    assert.equal(el.uptime.textContent, '00:02:05');
    binder.dispose();
});

test('TelemetryBinder: memory auto-scales to GB when large', () => {
    const state = new AppState();
    const el = makeTelemetryFixture();
    const binder = new TelemetryBinder({ state, elements: el });
    state.updateTelemetry({ memory: 2048, memoryMax: 4096 });
    assert.equal(el.memValue.textContent, '2.0');
    assert.equal(el.memUnit.textContent, 'GB');
    assert.equal(el.memBar.style.width, '50.0%');
    binder.dispose();
});

test('TelemetryBinder: coalesces burst into a single DOM write', () => {
    const state = new AppState();
    const el = makeTelemetryFixture();
    // Buffer raf so bursts accumulate then flush once.
    let queued = null;
    globalThis.requestAnimationFrame = (fn) => { queued = fn; return 1; };
    try {
        const binder = new TelemetryBinder({ state, elements: el });
        // Fire 5 bursts; only one raf should be queued.
        for (let i = 0; i < 5; i += 1) state.updateTelemetry({ cpu: 10 + i });
        // Still the initial scheduled render; value not yet flushed with latest.
        queued?.();
        assert.equal(el.cpuValue.textContent, '14.0');  // last cpu
        binder.dispose();
    } finally {
        globalThis.requestAnimationFrame = (fn) => { fn(); return 1; };
    }
});

test('TelemetryBinder: processes count comes from Map size', () => {
    const state = new AppState();
    const el = makeTelemetryFixture();
    const binder = new TelemetryBinder({ state, elements: el });
    state.updateProcess('p1', { state: 'running' });
    state.updateProcess('p2', { state: 'running' });
    assert.equal(el.procs.textContent, '2');
    binder.dispose();
});

// ─── PanelCollapseBinder integration ───────────────────────────────────────

function makeCollapseFixture() {
    const root = new StubElement('div');
    root.setAttribute('data-panels-collapsed', 'false');
    const toggle = new StubElement('button');
    toggle.setAttribute('aria-expanded', 'true');
    const scroller = new StubElement('div');
    // Nested focusable inside scroller
    const innerBtn = new StubElement('button');
    scroller.appendChild(innerBtn);
    return { root, toggle, scroller, innerBtn };
}

test('PanelCollapseBinder: click toggles data-panels-collapsed + aria', () => {
    const fx = makeCollapseFixture();
    const binder = new PanelCollapseBinder({
        root: fx.root, toggleButton: fx.toggle, scroller: fx.scroller,
        documentRef: stubDoc,
    });
    assert.equal(fx.root.getAttribute('data-panels-collapsed'), 'false');
    assert.equal(fx.toggle.getAttribute('aria-expanded'), 'true');
    fx.toggle.click();
    assert.equal(fx.root.getAttribute('data-panels-collapsed'), 'true');
    assert.equal(fx.toggle.getAttribute('aria-expanded'), 'false');
    assert.equal(fx.scroller.getAttribute('aria-hidden'), 'true');
    assert.equal('inert' in fx.scroller.attributes, true);
    fx.toggle.click();
    assert.equal(fx.root.getAttribute('data-panels-collapsed'), 'false');
    assert.equal('aria-hidden' in fx.scroller.attributes, false);
    assert.equal('inert' in fx.scroller.attributes, false);
    binder.dispose();
});

test('PanelCollapseBinder: Ctrl+B keydown triggers toggle', () => {
    const fx = makeCollapseFixture();
    const binder = new PanelCollapseBinder({
        root: fx.root, toggleButton: fx.toggle, scroller: fx.scroller,
        documentRef: stubDoc,
    });
    stubDoc.dispatchEvent({
        type: 'keydown', key: 'b', ctrlKey: true, metaKey: false,
        altKey: false, shiftKey: false, target: new StubElement('body'),
        preventDefault: () => {},
    });
    assert.equal(fx.root.getAttribute('data-panels-collapsed'), 'true');
    binder.dispose();
});

test('PanelCollapseBinder: Ctrl+B inside INPUT is ignored', () => {
    const fx = makeCollapseFixture();
    const binder = new PanelCollapseBinder({
        root: fx.root, toggleButton: fx.toggle, scroller: fx.scroller,
        documentRef: stubDoc,
    });
    const input = new StubElement('input');
    stubDoc.dispatchEvent({
        type: 'keydown', key: 'b', ctrlKey: true, metaKey: false,
        altKey: false, shiftKey: false, target: input,
        preventDefault: () => {},
    });
    assert.equal(fx.root.getAttribute('data-panels-collapsed'), 'false');
    binder.dispose();
});

test('PanelCollapseBinder: collapse with focus inside scroller moves focus to toggle', () => {
    const fx = makeCollapseFixture();
    const binder = new PanelCollapseBinder({
        root: fx.root, toggleButton: fx.toggle, scroller: fx.scroller,
        documentRef: stubDoc,
    });
    fx.innerBtn.focus();
    assert.equal(stubDoc.activeElement, fx.innerBtn);
    binder.toggle();   // collapse
    assert.equal(stubDoc.activeElement, fx.toggle);
    binder.dispose();
});

// ─── StatusBarBinder integration ───────────────────────────────────────────

function makeStatusFixture() {
    const indicator = new StubElement('span');
    const dot = new StubElement('span');
    dot.classList.add('ops-statusbar__dot');
    indicator.appendChild(dot);
    return {
        indicator,
        dot,
        connText: new StubElement('span'),
        hitlText: new StubElement('span'),
    };
}

test('StatusBarBinder: initial state → "Desconectado" + err dot', () => {
    const state = new AppState();
    const el = makeStatusFixture();
    const binder = new StatusBarBinder({ state, elements: el });
    assert.equal(el.connText.textContent, 'Desconectado');
    assert.equal(el.dot.classList.contains('ops-statusbar__dot--err'), true);
    binder.dispose();
});

test('StatusBarBinder: setConnection(connected) paints OK dot', () => {
    const state = new AppState();
    const el = makeStatusFixture();
    const binder = new StatusBarBinder({ state, elements: el });
    state.setConnection('connected', { attempts: 0 });
    assert.equal(el.connText.textContent, 'Conectado');
    assert.equal(el.dot.classList.contains('ops-statusbar__dot--ok'), true);
    assert.equal(el.dot.classList.contains('ops-statusbar__dot--err'), false);
    binder.dispose();
});

test('StatusBarBinder: reconnecting with attempts count', () => {
    const state = new AppState();
    const el = makeStatusFixture();
    const binder = new StatusBarBinder({ state, elements: el });
    state.setConnection('reconnecting', { attempts: 2 });
    assert.equal(el.connText.textContent, 'Reconectando (intento 2)');
    assert.equal(el.dot.classList.contains('ops-statusbar__dot--warn'), true);
    binder.dispose();
});

test('StatusBarBinder: conflict count updates HITL text', () => {
    const state = new AppState();
    const el = makeStatusFixture();
    const binder = new StatusBarBinder({ state, elements: el });
    assert.equal(el.hitlText.textContent, '0 alertas HITL');
    state.addConflict({ id: 'c1', title: 'first' });
    assert.equal(el.hitlText.textContent, '1 alerta HITL');
    state.addConflict({ id: 'c2', title: 'second' });
    assert.equal(el.hitlText.textContent, '2 alertas HITL');
    state.resolveConflict('c1');
    assert.equal(el.hitlText.textContent, '1 alerta HITL');
    binder.dispose();
});

test('StatusBarBinder: DOT_CLASSES exposes exactly the CSS modifier set', () => {
    assert.deepEqual([...DOT_CLASSES], [
        'ops-statusbar__dot--ok',
        'ops-statusbar__dot--warn',
        'ops-statusbar__dot--err',
    ]);
});


// ─── Summary ───────────────────────────────────────────────────────────────

if (process.exitCode) {
    // eslint-disable-next-line no-console
    console.error(`\nFAILED — ${passed} tests passing before first failure.`);
} else {
    // eslint-disable-next-line no-console
    console.log(`\nAll ${passed} tests passed.`);
}

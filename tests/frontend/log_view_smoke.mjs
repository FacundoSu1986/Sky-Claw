/* Smoke test for frontend/js/log-view.js.
 *
 * Runs in Node with:
 *   - Pure helper coverage: computeVisibleRange, isScrolledToBottom,
 *     shouldAutoScrollOnAppend, applyLevelFilter, formatLogTimestamp,
 *     levelDisplayText.
 *   - End-to-end LogView coverage using a minimal hand-rolled DOM stub
 *     (enough for replaceChildren / createElement / event dispatch /
 *     scrollTop mutation).
 *
 * Exits non-zero on any failure.
 * Usage:   node tests/frontend/log_view_smoke.mjs
 */

import { strict as assert } from 'node:assert';
import {
    ROW_HEIGHT,
    DEFAULT_OVERSCAN,
    LEVEL_DISPLAY,
    computeVisibleRange,
    isScrolledToBottom,
    shouldAutoScrollOnAppend,
    applyLevelFilter,
    formatLogTimestamp,
    levelDisplayText,
    LogView,
} from '../../frontend/js/log-view.js';
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

// ─── Pure helpers ──────────────────────────────────────────────────────────

test('ROW_HEIGHT is a positive integer (matches CSS)', () => {
    assert.equal(Number.isInteger(ROW_HEIGHT), true);
    assert.equal(ROW_HEIGHT > 0, true);
});

test('LEVEL_DISPLAY covers every level used in AppState', () => {
    assert.ok(LEVEL_DISPLAY.debug && LEVEL_DISPLAY.info && LEVEL_DISPLAY.success);
    assert.ok(LEVEL_DISPLAY.warning && LEVEL_DISPLAY.error);
});

test('computeVisibleRange: empty list → zero range', () => {
    const r = computeVisibleRange(0, 400, 0);
    assert.deepEqual(r, { startIdx: 0, endIdx: 0, offsetPx: 0 });
});

test('computeVisibleRange: top of a 5000-row list', () => {
    const r = computeVisibleRange(0, 400, 5000);
    assert.equal(r.startIdx, 0);
    // visibleCount = ceil(400/20) = 20, + overscan(8) above/below
    assert.equal(r.endIdx, 20 + DEFAULT_OVERSCAN);
    assert.equal(r.offsetPx, 0);
});

test('computeVisibleRange: mid-scroll math is O(1) and correct', () => {
    // Scroll down to row 1000: scrollTop = 1000*20 = 20000
    const r = computeVisibleRange(20000, 400, 5000);
    // firstVisible = 1000; start = 1000 - 8 = 992; end = 1000 + 20 + 8 = 1028
    assert.equal(r.startIdx, 992);
    assert.equal(r.endIdx, 1028);
    assert.equal(r.offsetPx, 992 * ROW_HEIGHT);
});

test('computeVisibleRange: bottom clamps endIdx at total', () => {
    // scrollTop near the bottom of 100 rows
    const r = computeVisibleRange(100 * ROW_HEIGHT - 400, 400, 100);
    assert.equal(r.endIdx, 100);
});

test('computeVisibleRange: custom rowHeight + overscan', () => {
    const r = computeVisibleRange(500, 300, 1000, { rowHeight: 25, overscan: 2 });
    // firstVisible = 20; visibleCount = 12; start = 18; end = 20+12+2 = 34
    assert.equal(r.startIdx, 18);
    assert.equal(r.endIdx, 34);
    assert.equal(r.offsetPx, 450);
});

test('isScrolledToBottom: content shorter than viewport → true', () => {
    assert.equal(isScrolledToBottom(0, 800, 100), true);
});

test('isScrolledToBottom: exact bottom → true', () => {
    assert.equal(isScrolledToBottom(4000, 400, 4400), true);
});

test('isScrolledToBottom: within tolerance of bottom → true', () => {
    // tolerance defaults to ROW_HEIGHT (20)
    assert.equal(isScrolledToBottom(4385, 400, 4400), true);
});

test('isScrolledToBottom: user scrolled up → false', () => {
    assert.equal(isScrolledToBottom(1000, 400, 4400), false);
});

test('shouldAutoScrollOnAppend: both true → true', () => {
    assert.equal(shouldAutoScrollOnAppend(true, true), true);
});

test('shouldAutoScrollOnAppend: user toggled off → false', () => {
    assert.equal(shouldAutoScrollOnAppend(false, true), false);
});

test('shouldAutoScrollOnAppend: user scrolled up → false', () => {
    assert.equal(shouldAutoScrollOnAppend(true, false), false);
});

test('applyLevelFilter: identity on all', () => {
    const logs = [{ level: 'info' }, { level: 'error' }];
    assert.equal(applyLevelFilter(logs, 'all'), logs);
});

test('applyLevelFilter: filters by exact level', () => {
    const logs = [
        { level: 'info', m: 1 },
        { level: 'warning', m: 2 },
        { level: 'error', m: 3 },
        { level: 'warning', m: 4 },
    ];
    const out = applyLevelFilter(logs, 'warning');
    assert.equal(out.length, 2);
    assert.deepEqual(out.map((x) => x.m), [2, 4]);
});

test('applyLevelFilter: invalid input safety', () => {
    assert.deepEqual(applyLevelFilter(null, 'info'), []);
    assert.deepEqual(applyLevelFilter(undefined, 'info'), []);
});

test('formatLogTimestamp: returns HH:MM:SS.mmm', () => {
    const out = formatLogTimestamp(new Date(2024, 0, 1, 3, 5, 9, 7).getTime());
    assert.match(out, /^\d{2}:\d{2}:\d{2}\.\d{3}$/);
    assert.equal(out, '03:05:09.007');
});

test('levelDisplayText: known levels return compact label', () => {
    assert.equal(levelDisplayText('warning'), 'WARN');
    assert.equal(levelDisplayText('error'), 'ERR');
    assert.equal(levelDisplayText('success'), 'OK');
    assert.equal(levelDisplayText('info'), 'INFO');
    assert.equal(levelDisplayText('debug'), 'DBG');
});

test('levelDisplayText: unknown level falls back to upper-case', () => {
    assert.equal(levelDisplayText('trace'), 'TRACE');
    assert.equal(levelDisplayText(null), '??');
});


// ─── Minimal DOM stub for LogView integration tests ────────────────────────

class StubElement {
    constructor(tag = 'div') {
        this.tagName = tag.toUpperCase();
        this.children = [];
        this.style = {};
        this.dataset = {};
        this.attributes = Object.create(null);
        this.className = '';
        this.textContent = '';
        this.title = '';
        this.listeners = Object.create(null);
        this.scrollTop = 0;
        this.clientHeight = 400;  // default viewport
        this.scrollHeight = 0;
        this.hidden = false;
    }
    appendChild(child) {
        this.children.push(child);
        child.parent = this;
        return child;
    }
    append(...kids) {
        for (const k of kids) this.appendChild(k);
    }
    replaceChildren(...kids) {
        this.children = [];
        // Support DocumentFragment stub (has .children collection)
        for (const k of kids) {
            if (k && Array.isArray(k.children)) {
                for (const c of k.children) this.appendChild(c);
            } else if (k != null) {
                this.appendChild(k);
            }
        }
    }
    setAttribute(name, value) { this.attributes[name] = String(value); }
    getAttribute(name) { return name in this.attributes ? this.attributes[name] : null; }
    removeAttribute(name) { delete this.attributes[name]; this.hidden = false; }
    addEventListener(event, fn) {
        (this.listeners[event] ??= []).push(fn);
    }
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
}

class StubDocument {
    createElement(tag) { return new StubElement(tag); }
    createDocumentFragment() {
        const frag = new StubElement('#fragment');
        return frag;
    }
}

function installDomStubs() {
    globalThis.document = new StubDocument();
    globalThis.requestAnimationFrame = (fn) => { fn(); return 1; };
    globalThis.cancelAnimationFrame = () => {};
}
installDomStubs();


function makeDomFixture() {
    const container = new StubElement('div');
    container.clientHeight = 400;
    const btnAll = Object.assign(new StubElement('button'), { });
    btnAll.dataset.level = 'all';
    btnAll.setAttribute('aria-pressed', 'true');
    const btnInfo = new StubElement('button'); btnInfo.dataset.level = 'info';
    btnInfo.setAttribute('aria-pressed', 'false');
    const btnWarn = new StubElement('button'); btnWarn.dataset.level = 'warning';
    btnWarn.setAttribute('aria-pressed', 'false');
    const btnErr = new StubElement('button'); btnErr.dataset.level = 'error';
    btnErr.setAttribute('aria-pressed', 'false');
    const btnDbg = new StubElement('button'); btnDbg.dataset.level = 'debug';
    btnDbg.setAttribute('aria-pressed', 'false');
    const clearBtn = new StubElement('button');
    const autoBtn = new StubElement('button');
    autoBtn.setAttribute('aria-pressed', 'true');
    const empty = new StubElement('div');
    empty.setAttribute('hidden', '');
    return {
        container,
        filterButtons: [btnAll, btnInfo, btnWarn, btnErr, btnDbg],
        clearButton: clearBtn,
        autoScrollButton: autoBtn,
        emptyState: empty,
    };
}

// ─── LogView integration tests ─────────────────────────────────────────────

test('LogView: mounts and renders empty state when no logs', () => {
    const state = new AppState();
    const dom = makeDomFixture();
    const view = new LogView({ state, ...dom });
    // Spacer + window injected
    assert.equal(dom.container.children.length, 1);
    const spacer = dom.container.children[0];
    assert.equal(spacer.style.height, '0px');
    // Empty state is visible (no 'hidden' attr)
    assert.equal('hidden' in dom.emptyState.attributes, false);
    view.dispose();
});

test('LogView: appendLog produces a visible row', () => {
    const state = new AppState();
    const dom = makeDomFixture();
    const view = new LogView({ state, ...dom });
    state.appendLog({ level: 'info', message: 'hello world' });
    const spacer = dom.container.children[0];
    const windowEl = spacer.children[0];
    assert.equal(windowEl.children.length, 1);
    const row = windowEl.children[0];
    assert.match(row.className, /ops-log-line--info/);
    // Row has three spans: time, level, msg
    const textNodes = row.children.map((c) => c.textContent);
    assert.equal(textNodes.length, 3);
    assert.equal(textNodes[1], 'INFO');
    assert.equal(textNodes[2], 'hello world');
    view.dispose();
});

test('LogView: filter button click hides non-matching rows', () => {
    const state = new AppState();
    const dom = makeDomFixture();
    const view = new LogView({ state, ...dom });
    state.appendLog({ level: 'info', message: 'i1' });
    state.appendLog({ level: 'error', message: 'e1' });
    state.appendLog({ level: 'warning', message: 'w1' });
    // Three rows visible under 'all'
    let windowEl = dom.container.children[0].children[0];
    assert.equal(windowEl.children.length, 3);
    // Click 'error' filter
    const errBtn = dom.filterButtons.find((b) => b.dataset.level === 'error');
    errBtn.click();
    windowEl = dom.container.children[0].children[0];
    assert.equal(windowEl.children.length, 1);
    assert.equal(windowEl.children[0].children[2].textContent, 'e1');
    // aria-pressed updated
    assert.equal(errBtn.getAttribute('aria-pressed'), 'true');
    assert.equal(dom.filterButtons[0].getAttribute('aria-pressed'), 'false');
    view.dispose();
});

test('LogView: sticky auto-scroll snaps to bottom on append when at bottom', () => {
    const state = new AppState();
    const dom = makeDomFixture();
    const view = new LogView({ state, ...dom });
    // Start at bottom (scrollTop=0, total=0 → trivially at bottom)
    for (let i = 0; i < 50; i += 1) {
        state.appendLog({ level: 'info', message: `m${i}` });
    }
    // With 50 rows and rowHeight 20, scrollTop should have snapped to 50*20 = 1000
    assert.equal(dom.container.scrollTop, 1000);
    view.dispose();
});

test('LogView: sticky pauses when user scrolled up', () => {
    const state = new AppState();
    const dom = makeDomFixture();
    const view = new LogView({ state, ...dom });
    // Seed 50 entries so there is scrollback
    for (let i = 0; i < 50; i += 1) {
        state.appendLog({ level: 'info', message: `m${i}` });
    }
    // User scrolls up
    dom.container.scrollTop = 100;
    dom.container.scrollHeight = 50 * ROW_HEIGHT;
    dom.container.dispatchEvent({ type: 'scroll' });
    // New logs must NOT snap scrollTop to bottom
    state.appendLog({ level: 'info', message: 'new-while-scrolled-up' });
    assert.equal(dom.container.scrollTop, 100);
    view.dispose();
});

test('LogView: returning to bottom re-enables sticky', () => {
    const state = new AppState();
    const dom = makeDomFixture();
    const view = new LogView({ state, ...dom });
    for (let i = 0; i < 50; i += 1) {
        state.appendLog({ level: 'info', message: `m${i}` });
    }
    // Scroll up (sticky paused)
    dom.container.scrollTop = 100;
    dom.container.scrollHeight = 50 * ROW_HEIGHT;
    dom.container.dispatchEvent({ type: 'scroll' });
    state.appendLog({ level: 'info', message: 'no-snap' });
    assert.equal(dom.container.scrollTop, 100);
    // User scrolls back to bottom
    dom.container.scrollTop = 51 * ROW_HEIGHT - 400;
    dom.container.scrollHeight = 51 * ROW_HEIGHT;
    dom.container.dispatchEvent({ type: 'scroll' });
    // Next append should snap again
    state.appendLog({ level: 'info', message: 'snap-again' });
    // Total now 52 rows → expected scrollTop = 52 * ROW_HEIGHT
    assert.equal(dom.container.scrollTop, 52 * ROW_HEIGHT);
    view.dispose();
});

test('LogView: autoscroll toggle button disables sticky', () => {
    const state = new AppState();
    const dom = makeDomFixture();
    const view = new LogView({ state, ...dom });
    // Disable via toggle
    dom.autoScrollButton.click();
    assert.equal(dom.autoScrollButton.getAttribute('aria-pressed'), 'false');
    // Stay at bottom
    for (let i = 0; i < 10; i += 1) {
        state.appendLog({ level: 'info', message: `m${i}` });
    }
    // scrollTop should still be 0 (no snap, because userAutoScroll=false)
    assert.equal(dom.container.scrollTop, 0);
    view.dispose();
});

test('LogView: clear button empties the view and re-shows empty state', () => {
    const state = new AppState();
    const dom = makeDomFixture();
    const view = new LogView({ state, ...dom });
    state.appendLog({ level: 'info', message: 'will-vanish' });
    assert.equal(dom.container.children[0].children[0].children.length, 1);
    dom.clearButton.click();
    assert.equal(dom.container.children[0].children[0].children.length, 0);
    assert.equal('hidden' in dom.emptyState.attributes, false);
    view.dispose();
});

test('LogView: large append (5000 rows) renders bounded window only', () => {
    const state = new AppState({ logsCapacity: 5000 });
    const dom = makeDomFixture();
    const view = new LogView({ state, ...dom });
    for (let i = 0; i < 5000; i += 1) {
        state.appendLog({ level: 'info', message: `line-${i}` });
    }
    const windowEl = dom.container.children[0].children[0];
    // With clientHeight=400, overscan=8 → at most ~36 rows rendered
    assert.ok(windowEl.children.length <= 40, `rendered too many rows: ${windowEl.children.length}`);
    // Spacer height matches total
    assert.equal(dom.container.children[0].style.height, `${5000 * ROW_HEIGHT}px`);
    view.dispose();
});

// ─── Summary ───────────────────────────────────────────────────────────────

if (process.exitCode) {
    // eslint-disable-next-line no-console
    console.error(`\nFAILED — ${passed} tests passing before first failure.`);
} else {
    // eslint-disable-next-line no-console
    console.log(`\nAll ${passed} tests passed.`);
}

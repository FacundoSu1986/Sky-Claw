/* Smoke test for frontend/js/appstate.js + operations-hub.js routing.
 *
 * Runs under Node (no browser needed) to validate:
 *   1. RingBuffer overflow keeps the latest N and drops the oldest.
 *   2. AppState fires observers only for the mutated key.
 *   3. Unsubscribe stops notifications.
 *   4. applyFrameToState routes ops.log.* → logs, ops.telemetry.* → telemetry,
 *      ops.process.* → processes, ops.hitl.* → conflicts.
 *   5. snapshot / pong meta frames don't produce log entries.
 *   6. conflict.resolved removes the entry.
 *
 * Exits with code 0 on success, non-zero on any failure.
 * Invoke with:   node tests/frontend/appstate_smoke.mjs
 */

import { strict as assert } from 'node:assert';
import { AppState, RingBuffer } from '../../frontend/js/appstate.js';
import { applyFrameToState, matchesTopic } from '../../frontend/js/operations-hub.js';

let passed = 0;
function test(name, fn) {
    try {
        fn();
        passed += 1;
        // eslint-disable-next-line no-console
        console.log(`  OK  ${name}`);
    } catch (err) {
        // eslint-disable-next-line no-console
        console.error(`  FAIL  ${name}\n    ${err.message}`);
        process.exitCode = 1;
    }
}

// ─── RingBuffer ────────────────────────────────────────────────────────────

test('RingBuffer push within capacity keeps insertion order', () => {
    const rb = new RingBuffer(3);
    rb.push('a'); rb.push('b'); rb.push('c');
    assert.deepEqual(rb.toArray(), ['a', 'b', 'c']);
    assert.equal(rb.length, 3);
});

test('RingBuffer overflow drops oldest (FIFO)', () => {
    const rb = new RingBuffer(3);
    for (const x of ['a', 'b', 'c', 'd', 'e']) rb.push(x);
    assert.deepEqual(rb.toArray(), ['c', 'd', 'e']);
    assert.equal(rb.length, 3);
});

test('RingBuffer.push returns evicted item once full', () => {
    const rb = new RingBuffer(2);
    assert.equal(rb.push('a'), undefined);
    assert.equal(rb.push('b'), undefined);
    assert.equal(rb.push('c'), 'a');
});

test('RingBuffer.clear resets size and contents', () => {
    const rb = new RingBuffer(3);
    rb.push(1); rb.push(2);
    rb.clear();
    assert.equal(rb.length, 0);
    assert.deepEqual(rb.toArray(), []);
});

test('RingBuffer rejects invalid capacity', () => {
    assert.throws(() => new RingBuffer(0), /positive/);
    assert.throws(() => new RingBuffer(-1), /positive/);
});

test('RingBuffer scales: 5000 push + append-one evicts correctly', () => {
    const rb = new RingBuffer(5000);
    for (let i = 0; i < 5000; i += 1) rb.push(i);
    rb.push(5000);
    const arr = rb.toArray();
    assert.equal(arr.length, 5000);
    assert.equal(arr[0], 1);
    assert.equal(arr[4999], 5000);
});

// ─── AppState observers ────────────────────────────────────────────────────

test('AppState.subscribe fires on appendLog', () => {
    const state = new AppState({ logsCapacity: 10 });
    let calls = 0;
    let lastSnap = null;
    state.subscribe('logs', (snap) => { calls += 1; lastSnap = snap; });
    state.appendLog({ level: 'info', message: 'hello' });
    assert.equal(calls, 1);
    assert.equal(lastSnap.length, 1);
    assert.equal(lastSnap[0].message, 'hello');
});

test('AppState unsubscribe stops further notifications', () => {
    const state = new AppState();
    let calls = 0;
    const off = state.subscribe('logs', () => { calls += 1; });
    state.appendLog({ message: 'one' });
    off();
    state.appendLog({ message: 'two' });
    assert.equal(calls, 1);
});

test('AppState notifies only the relevant key bucket', () => {
    const state = new AppState();
    let logsCalls = 0;
    let telemetryCalls = 0;
    state.subscribe('logs', () => { logsCalls += 1; });
    state.subscribe('telemetry', () => { telemetryCalls += 1; });
    state.appendLog({ message: 'x' });
    assert.equal(logsCalls, 1);
    assert.equal(telemetryCalls, 0);
    state.updateTelemetry({ cpu: 12.3 });
    assert.equal(logsCalls, 1);
    assert.equal(telemetryCalls, 1);
});

test('AppState.updateProcess + removeProcess', () => {
    const state = new AppState();
    let procSnap = null;
    state.subscribe('processes', (p) => { procSnap = p; });
    state.updateProcess('mo2', { state: 'running', label: 'MO2' });
    assert.equal(procSnap.get('mo2').state, 'running');
    state.updateProcess('mo2', { state: 'finished' });
    assert.equal(procSnap.get('mo2').state, 'finished');
    state.removeProcess('mo2');
    assert.equal(procSnap.has('mo2'), false);
});

test('AppState.clearLogs empties the ring buffer', () => {
    const state = new AppState({ logsCapacity: 4 });
    for (let i = 0; i < 3; i += 1) state.appendLog({ message: `m${i}` });
    assert.equal(state.logs.length, 3);
    state.clearLogs();
    assert.equal(state.logs.length, 0);
});

test('AppState ring-buffer bound at logsCapacity', () => {
    const state = new AppState({ logsCapacity: 4 });
    for (let i = 0; i < 10; i += 1) state.appendLog({ message: `m${i}` });
    const snap = state.logs;
    assert.equal(snap.length, 4);
    assert.equal(snap[0].message, 'm6');
    assert.equal(snap[3].message, 'm9');
});

// ─── Topic matcher ─────────────────────────────────────────────────────────

test('matchesTopic wildcard suffix', () => {
    assert.equal(matchesTopic('ops.log.info', 'ops.log.*'), true);
    assert.equal(matchesTopic('ops.log.debug.sub', 'ops.log.*'), true);
    assert.equal(matchesTopic('ops.log', 'ops.log.*'), false);
    assert.equal(matchesTopic('ops.telemetry.snapshot', 'ops.log.*'), false);
    assert.equal(matchesTopic('same', 'same'), true);
});

// ─── applyFrameToState routing ─────────────────────────────────────────────

test('applyFrameToState: snapshot → connection=connected', () => {
    const state = new AppState();
    applyFrameToState(state, { event_type: 'snapshot', payload: { connected: true } });
    assert.equal(state.connection.status, 'connected');
});

test('applyFrameToState: pong does not pollute logs', () => {
    const state = new AppState();
    applyFrameToState(state, { event_type: 'pong', payload: {} });
    assert.equal(state.logs.length, 0);
});

test('applyFrameToState: ops.log.info → appendLog (info)', () => {
    const state = new AppState();
    applyFrameToState(state, {
        event_type: 'ops.log.info',
        payload: { message: 'plugin scan complete', level: 'info' },
        source: 'tool_dispatcher',
        timestamp_ms: 1700000000000,
    });
    assert.equal(state.logs.length, 1);
    assert.equal(state.logs[0].level, 'info');
    assert.equal(state.logs[0].message, 'plugin scan complete');
    assert.equal(state.logs[0].source, 'tool_dispatcher');
});

test('applyFrameToState: ops.log.warning maps warn→warning', () => {
    const state = new AppState();
    applyFrameToState(state, {
        event_type: 'ops.log.warning',
        payload: { message: 'slow response', level: 'warn' },
    });
    assert.equal(state.logs[0].level, 'warning');
});

test('applyFrameToState: ops.process.* updates processes', () => {
    const state = new AppState();
    applyFrameToState(state, {
        event_type: 'ops.process.started',
        payload: { process_id: 'dyndolod', state: 'running', label: 'DynDOLOD' },
    });
    assert.equal(state.processes.get('dyndolod').state, 'running');
});

test('applyFrameToState: ops.telemetry.snapshot updates telemetry', () => {
    const state = new AppState();
    applyFrameToState(state, {
        event_type: 'ops.telemetry.snapshot',
        payload: { cpu: 42.5, memory_mb: 1024, active_processes: 3, uptime_s: 60 },
    });
    assert.equal(state.telemetry.cpu, 42.5);
    assert.equal(state.telemetry.memory, 1024);
    assert.equal(state.telemetry.activeProcesses, 3);
    assert.equal(state.telemetry.uptimeMs, 60_000);
});

test('applyFrameToState: ops.hitl.new → addConflict, .resolved → remove', () => {
    const state = new AppState();
    applyFrameToState(state, {
        event_type: 'ops.hitl.new',
        payload: { id: 'c1', title: 'Conflict between A and B', severity: 'error' },
    });
    assert.equal(state.conflicts.length, 1);
    assert.equal(state.conflicts[0].title, 'Conflict between A and B');

    applyFrameToState(state, {
        event_type: 'ops.hitl.resolved',
        payload: { id: 'c1' },
    });
    assert.equal(state.conflicts.length, 0);
});

test('applyFrameToState: legacy system.telemetry.* bridges to telemetry', () => {
    const state = new AppState();
    applyFrameToState(state, {
        event_type: 'system.telemetry.update',
        payload: { cpu_percent: 7.2, memory_mb: 512 },
    });
    assert.equal(state.telemetry.cpu, 7.2);
    assert.equal(state.telemetry.memory, 512);
});

test('applyFrameToState: frame with no event_type is ignored', () => {
    const state = new AppState();
    applyFrameToState(state, null);
    applyFrameToState(state, {});
    applyFrameToState(state, { payload: { message: 'orphan' } });
    assert.equal(state.logs.length, 0);
});

// ─── Summary ───────────────────────────────────────────────────────────────

if (process.exitCode) {
    // eslint-disable-next-line no-console
    console.error(`\nFAILED — ${passed} passing before first failure.`);
} else {
    // eslint-disable-next-line no-console
    console.log(`\nAll ${passed} tests passed.`);
}

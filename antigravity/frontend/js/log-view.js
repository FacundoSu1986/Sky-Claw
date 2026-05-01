/* ═══════════════════════════════════════════════════════════════════════════════════
   SKY-CLAW — OPERATIONS HUB · Log view (Orbe de Visión)
   Virtual-scrolling, sticky-auto-scroll, level-filtered log viewer.

   Design constraints (Phase 4 directives):
     • Fixed row height (ROW_HEIGHT) so start/end indexes are O(1) math and
       the browser paints 5000 rows at 60fps without layout thrash.
     • Sticky auto-scroll: if the user scrolls up to read history, auto-scroll
       pauses. It resumes automatically when the user scrolls back to the
       bottom. An explicit toggle button gives the user a hard override.
     • Filter switches operate on the `_filteredLogs` array only — the visible
       window is re-rendered, the other 4,900+ rows never touch the DOM.

   Pure helpers (computeVisibleRange, shouldAutoScrollOnAppend,
   applyLevelFilter, formatLogTimestamp, levelDisplayText) are exported so
   Node can unit-test the math without a DOM.
   ═══════════════════════════════════════════════════════════════════════════════════ */

/** Strict row height in pixels; MUST match the CSS `.ops-log-line { height }`. */
export const ROW_HEIGHT = 20;

/** Overscan rows rendered above/below the viewport for smooth scrolling. */
export const DEFAULT_OVERSCAN = 8;

/** "At bottom" detection tolerance (px). One row of slack feels natural. */
export const STICKY_TOLERANCE_PX = ROW_HEIGHT;

/** Accepted log levels and how they display in the stream column. */
export const LEVEL_DISPLAY = Object.freeze({
    debug: 'DBG',
    info: 'INFO',
    success: 'OK',
    warning: 'WARN',
    error: 'ERR',
});


// ───────────────────────────── Pure helpers ─────────────────────────────────

/**
 * Compute which rows should be rendered for the current scroll position.
 * O(1) — no iteration over data.
 *
 * @param {number} scrollTop      Container scrollTop in px.
 * @param {number} clientHeight   Container clientHeight in px.
 * @param {number} total          Total number of rows (after filter).
 * @param {object} [options]
 * @param {number} [options.rowHeight=ROW_HEIGHT]
 * @param {number} [options.overscan=DEFAULT_OVERSCAN]
 * @returns {{startIdx: number, endIdx: number, offsetPx: number}}
 */
export function computeVisibleRange(scrollTop, clientHeight, total, {
    rowHeight = ROW_HEIGHT,
    overscan = DEFAULT_OVERSCAN,
} = {}) {
    if (total <= 0 || clientHeight <= 0) {
        return { startIdx: 0, endIdx: 0, offsetPx: 0 };
    }
    const safeScrollTop = Math.max(0, Number.isFinite(scrollTop) ? scrollTop : 0);
    const firstVisible = Math.floor(safeScrollTop / rowHeight);
    const visibleCount = Math.ceil(clientHeight / rowHeight);
    const startIdx = Math.max(0, firstVisible - overscan);
    const endIdx = Math.min(total, firstVisible + visibleCount + overscan);
    const offsetPx = startIdx * rowHeight;
    return { startIdx, endIdx, offsetPx };
}


/**
 * Return true when the container is scrolled to (or very near) the bottom.
 *
 * @param {number} scrollTop
 * @param {number} clientHeight
 * @param {number} scrollHeight
 * @param {number} [tolerancePx=STICKY_TOLERANCE_PX]
 * @returns {boolean}
 */
export function isScrolledToBottom(scrollTop, clientHeight, scrollHeight, tolerancePx = STICKY_TOLERANCE_PX) {
    if (scrollHeight <= clientHeight) return true; // content fits entirely
    return scrollTop + clientHeight >= scrollHeight - tolerancePx;
}


/**
 * Decide whether an append of new logs should trigger an auto-scroll.
 *
 * Sticky contract:
 *   - `userEnabled=false` → always false (explicit override wins).
 *   - `userEnabled=true`  → only when the user was already at (or near) the
 *     bottom before the append.
 *
 * @param {boolean} userEnabled
 * @param {boolean} wasAtBottom
 * @returns {boolean}
 */
export function shouldAutoScrollOnAppend(userEnabled, wasAtBottom) {
    return Boolean(userEnabled) && Boolean(wasAtBottom);
}


/**
 * Filter a log snapshot by level. `'all'` is the identity filter.
 *
 * @param {Array<{level: string}>} logs
 * @param {string} level
 * @returns {Array}
 */
export function applyLevelFilter(logs, level) {
    if (!Array.isArray(logs)) return [];
    if (!level || level === 'all') return logs;
    return logs.filter((entry) => entry && entry.level === level);
}


/**
 * Format a millisecond timestamp as HH:MM:SS.mmm (24h, local time).
 *
 * @param {number} ms
 * @returns {string}
 */
export function formatLogTimestamp(ms) {
    const d = Number.isFinite(ms) ? new Date(ms) : new Date();
    const hh = String(d.getHours()).padStart(2, '0');
    const mm = String(d.getMinutes()).padStart(2, '0');
    const ss = String(d.getSeconds()).padStart(2, '0');
    const mmm = String(d.getMilliseconds()).padStart(3, '0');
    return `${hh}:${mm}:${ss}.${mmm}`;
}


/**
 * Map an internal level string to its compact display label.
 *
 * @param {string} level
 * @returns {string}
 */
export function levelDisplayText(level) {
    return LEVEL_DISPLAY[level] ?? (typeof level === 'string' ? level.toUpperCase() : '??');
}


// ───────────────────────────── LogView class ────────────────────────────────

/**
 * Virtual-scrolling, sticky-auto-scroll log viewer bound to an AppState.
 *
 * All DOM queries are driven by selectors passed at construction so the
 * component is easy to relocate or duplicate.
 *
 * Example:
 *     const hub = window.SkyClawOperationsHub;
 *     new LogView({
 *         state: hub.state,
 *         container: document.getElementById('ops-log-stream'),
 *         filterButtons: document.querySelectorAll('.ops-orbe__filter-btn'),
 *         clearButton: document.getElementById('ops-clear-logs'),
 *         autoScrollButton: document.getElementById('ops-autoscroll'),
 *         emptyState: document.getElementById('ops-orbe-empty'),
 *     });
 */
export class LogView {
    /**
     * @param {object} opts
     * @param {import('./appstate.js').AppState} opts.state
     * @param {HTMLElement} opts.container           Scroll container (#ops-log-stream).
     * @param {NodeListOf<HTMLElement>|HTMLElement[]} opts.filterButtons
     * @param {HTMLElement} [opts.clearButton]
     * @param {HTMLElement} [opts.autoScrollButton]
     * @param {HTMLElement} [opts.emptyState]
     * @param {number} [opts.rowHeight=ROW_HEIGHT]
     * @param {number} [opts.overscan=DEFAULT_OVERSCAN]
     */
    constructor({
        state,
        container,
        filterButtons,
        clearButton = null,
        autoScrollButton = null,
        emptyState = null,
        rowHeight = ROW_HEIGHT,
        overscan = DEFAULT_OVERSCAN,
    }) {
        if (!state || typeof state.subscribe !== 'function') {
            throw new TypeError('LogView requires an AppState-like object with subscribe()');
        }
        if (!container) {
            throw new TypeError('LogView requires a container element');
        }
        this._state = state;
        this._container = container;
        this._filterButtons = Array.from(filterButtons ?? []);
        this._clearButton = clearButton;
        this._autoScrollButton = autoScrollButton;
        this._emptyState = emptyState;
        this._rowHeight = rowHeight;
        this._overscan = overscan;

        /** @private */ this._filter = this._readInitialFilter();
        /** @private */ this._userAutoScroll = this._readInitialAutoScroll();
        /** @private */ this._isAtBottom = true;
        /** @private */ this._allLogs = [];
        /** @private */ this._filteredLogs = [];
        /** @private */ this._renderPending = false;
        /** @private */ this._pendingFrameHandle = null;

        this._buildDom();
        this._bindEvents();

        // Seed with any entries already buffered on the state.
        this._allLogs = this._state.logs;
        this._applyFilter();
        this._unsubscribe = this._state.subscribe('logs', (snap) => this._onLogs(snap));
        this._scheduleRender({ newLogs: false });
    }

    /** Unsubscribe and clean up listeners. */
    dispose() {
        if (this._unsubscribe) this._unsubscribe();
        if (this._pendingFrameHandle !== null && typeof cancelAnimationFrame === 'function') {
            cancelAnimationFrame(this._pendingFrameHandle);
        }
        this._pendingFrameHandle = null;
        this._renderPending = false;
        this._container.removeEventListener('scroll', this._onScrollBound);
        for (const [btn, handler] of this._filterHandlers) {
            btn.removeEventListener('click', handler);
        }
        if (this._clearButton && this._onClearBound) {
            this._clearButton.removeEventListener('click', this._onClearBound);
        }
        if (this._autoScrollButton && this._onAutoScrollBound) {
            this._autoScrollButton.removeEventListener('click', this._onAutoScrollBound);
        }
    }

    // ──────────────────────────── Setup ────────────────────────────────────

    _readInitialFilter() {
        const pressed = this._filterButtons?.find?.((b) => b.getAttribute?.('aria-pressed') === 'true');
        return pressed?.dataset?.level || 'all';
    }

    _readInitialAutoScroll() {
        if (!this._autoScrollButton) return true;
        return this._autoScrollButton.getAttribute('aria-pressed') !== 'false';
    }

    _buildDom() {
        // Wipe Phase-1 placeholders and install virtual-scroll wrapper.
        this._container.replaceChildren();
        this._spacer = document.createElement('div');
        this._spacer.className = 'ops-orbe__vscroll-spacer';
        this._window = document.createElement('div');
        this._window.className = 'ops-orbe__vscroll-window';
        this._spacer.appendChild(this._window);
        this._container.appendChild(this._spacer);
    }

    _bindEvents() {
        this._onScrollBound = () => this._onScroll();
        this._container.addEventListener('scroll', this._onScrollBound, { passive: true });

        this._filterHandlers = new Map();
        for (const btn of this._filterButtons) {
            const handler = () => this._onFilterClick(btn);
            btn.addEventListener('click', handler);
            this._filterHandlers.set(btn, handler);
        }

        if (this._clearButton) {
            this._onClearBound = () => this._state.clearLogs();
            this._clearButton.addEventListener('click', this._onClearBound);
        }

        if (this._autoScrollButton) {
            this._onAutoScrollBound = () => this._onAutoScrollToggle();
            this._autoScrollButton.addEventListener('click', this._onAutoScrollBound);
        }
    }

    // ──────────────────────────── State sync ───────────────────────────────

    _onLogs(snap) {
        // Capture "was at bottom" BEFORE swapping data so sticky logic is accurate.
        const wasAtBottom = this._isAtBottom;
        this._allLogs = snap;
        this._applyFilter();
        this._scheduleRender({ newLogs: true, wasAtBottom });
    }

    _applyFilter() {
        this._filteredLogs = applyLevelFilter(this._allLogs, this._filter);
    }

    // ──────────────────────────── Rendering ────────────────────────────────

    _scheduleRender({ newLogs = false, wasAtBottom = this._isAtBottom } = {}) {
        // `_renderPending` is the single source of truth for "a frame is queued";
        // the opaque handle is kept only for cancellation. Separating the two
        // matters when raf invokes synchronously (tests, some older browsers):
        // the callback can null `_renderPending` BEFORE the handle is assigned,
        // letting the next schedule proceed as expected.
        if (this._renderPending) return;
        this._renderPending = true;
        const raf = (typeof requestAnimationFrame === 'function')
            ? requestAnimationFrame
            : (fn) => setTimeout(fn, 16);
        const handle = raf(() => {
            this._renderPending = false;
            this._pendingFrameHandle = null;
            // Auto-scroll BEFORE computing range so the range reflects the new scrollTop.
            if (newLogs && shouldAutoScrollOnAppend(this._userAutoScroll, wasAtBottom)) {
                this._scrollToBottom();
            }
            this._render();
        });
        if (this._renderPending) {
            // The callback hasn't run yet (real async raf) — retain handle for cancel.
            this._pendingFrameHandle = handle;
        }
    }

    _render() {
        const total = this._filteredLogs.length;
        const totalHeightPx = total * this._rowHeight;
        this._spacer.style.height = `${totalHeightPx}px`;

        if (total === 0) {
            this._window.replaceChildren();
            this._window.style.transform = 'translateY(0px)';
            this._toggleEmptyState(true);
            return;
        }
        this._toggleEmptyState(false);

        const { startIdx, endIdx, offsetPx } = computeVisibleRange(
            this._container.scrollTop,
            this._container.clientHeight,
            total,
            { rowHeight: this._rowHeight, overscan: this._overscan },
        );

        this._window.style.transform = `translateY(${offsetPx}px)`;
        this._renderRows(startIdx, endIdx);
    }

    _renderRows(startIdx, endIdx) {
        const frag = document.createDocumentFragment();
        for (let i = startIdx; i < endIdx; i += 1) {
            const entry = this._filteredLogs[i];
            if (entry) frag.appendChild(this._buildRow(entry));
        }
        this._window.replaceChildren(frag);
    }

    _buildRow(entry) {
        const level = entry.level ?? 'info';
        const row = document.createElement('div');
        row.className = `ops-log-line ops-log-line--${level}`;

        const time = document.createElement('span');
        time.className = 'ops-log-line__time';
        time.textContent = formatLogTimestamp(entry.timestampMs);

        const lvl = document.createElement('span');
        lvl.className = 'ops-log-line__level';
        lvl.textContent = levelDisplayText(level);

        const msg = document.createElement('span');
        msg.className = 'ops-log-line__msg';
        const text = String(entry.message ?? '');
        msg.textContent = text;
        msg.title = text; // hover shows full line when truncated

        row.append(time, lvl, msg);
        return row;
    }

    _toggleEmptyState(showEmpty) {
        if (!this._emptyState) return;
        if (showEmpty) {
            this._emptyState.removeAttribute('hidden');
        } else {
            this._emptyState.setAttribute('hidden', '');
        }
    }

    // ──────────────────────────── Events ───────────────────────────────────

    _onScroll() {
        const { scrollTop, clientHeight, scrollHeight } = this._container;
        this._isAtBottom = isScrolledToBottom(scrollTop, clientHeight, scrollHeight);
        // Re-render visible window for the new scrollTop.
        this._scheduleRender({ newLogs: false });
    }

    _onFilterClick(button) {
        const level = button?.dataset?.level ?? 'all';
        if (level === this._filter) return;
        this._filter = level;
        for (const btn of this._filterButtons) {
            btn.setAttribute('aria-pressed', btn.dataset.level === level ? 'true' : 'false');
        }
        const wasAtBottom = this._isAtBottom;
        this._applyFilter();
        this._scheduleRender({ newLogs: wasAtBottom, wasAtBottom });
    }

    _onAutoScrollToggle() {
        this._userAutoScroll = !this._userAutoScroll;
        if (this._autoScrollButton) {
            this._autoScrollButton.setAttribute('aria-pressed', String(this._userAutoScroll));
        }
        if (this._userAutoScroll) {
            this._scrollToBottom();
        }
    }

    _scrollToBottom() {
        // Set to a value larger than the current scrollHeight so the browser
        // clamps to the bottom even if spacer height hasn't been committed yet.
        const total = this._filteredLogs.length;
        const targetTop = Math.max(0, total * this._rowHeight);
        this._container.scrollTop = targetTop;
        this._isAtBottom = true;
    }
}

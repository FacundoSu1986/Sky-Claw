/* ═══════════════════════════════════════════════════════════════════════════════════
   SKY-CLAW — OPERATIONS HUB · Right-panel collapse/expand binder
   Manages the Arsenal+Telemetría side panels' collapsed state, synchronised
   across:
     • `data-panels-collapsed` attribute on the root (drives the CSS grid
       re-flow — 320px → 52px).
     • `aria-expanded` on the collapse/expand toggle button.
     • `aria-hidden` + `inert` on the scroll container so assistive tech and
       Tab navigation skip the collapsed region.
     • Focus management: if the user collapses while focus was inside the
       scroller, focus moves back to the toggle button to avoid a lost
       keyboard cursor (otherwise aria-hidden would hide the focused node,
       which screen readers announce as "unlabeled region").

   Directives (Fase 5):
     • Keyboard shortcut Ctrl+B / Cmd+B toggles the panel from anywhere
       (mirrors VSCode's sidebar convention).
     • The shortcut is cancelled inside text inputs so typing "Ctrl+B"
       inside a future ops.config form doesn't hijack it.
   ═══════════════════════════════════════════════════════════════════════════════════ */

/** Return true when the given element is a text-entry target. */
function isTextInput(el) {
    if (!el || !el.tagName) return false;
    const tag = el.tagName.toUpperCase();
    if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return true;
    if (el.isContentEditable) return true;
    return false;
}


/**
 * Compute whether a keyboard event is the collapse shortcut (Ctrl+B or
 * Cmd+B). Kept pure so tests don't need to synthesise KeyboardEvents.
 *
 * @param {{key?: string, ctrlKey?: boolean, metaKey?: boolean, altKey?: boolean, shiftKey?: boolean}} ev
 * @returns {boolean}
 */
export function isCollapseShortcut(ev) {
    if (!ev || ev.altKey || ev.shiftKey) return false;
    if (!ev.ctrlKey && !ev.metaKey) return false;
    const k = (ev.key ?? '').toLowerCase();
    return k === 'b';
}


/**
 * Binder for the right-panel collapse toggle.
 *
 * Example:
 *     new PanelCollapseBinder({
 *         root:         document.getElementById('ops-hub'),
 *         toggleButton: document.getElementById('ops-collapse-toggle'),
 *         scroller:     document.getElementById('ops-panels-scroller'),
 *     });
 */
export class PanelCollapseBinder {
    /**
     * @param {object} opts
     * @param {HTMLElement} opts.root           Root element with data-panels-collapsed.
     * @param {HTMLElement} opts.toggleButton   Button that flips the state.
     * @param {HTMLElement} [opts.scroller]     The region being collapsed.
     * @param {Document}    [opts.documentRef]  Override for tests.
     */
    constructor({ root, toggleButton, scroller = null, documentRef = null }) {
        if (!root) throw new TypeError('PanelCollapseBinder requires a root element');
        if (!toggleButton) throw new TypeError('PanelCollapseBinder requires a toggleButton');
        this._root = root;
        this._toggle = toggleButton;
        this._scroller = scroller;
        this._doc = documentRef || (typeof document !== 'undefined' ? document : null);

        // Derive initial state from the attribute already on the DOM (HTML
        // is authoritative; the binder just keeps things in sync afterward).
        this._collapsed = root.getAttribute('data-panels-collapsed') === 'true';

        this._onToggleClick = () => this.toggle();
        this._onKeyDown = (ev) => this._handleKeyDown(ev);

        this._toggle.addEventListener('click', this._onToggleClick);
        if (this._doc && typeof this._doc.addEventListener === 'function') {
            this._doc.addEventListener('keydown', this._onKeyDown);
        }

        // Initial attribute sync so HTML + a11y state always agree.
        this._applyState();
    }

    /** Remove listeners. */
    dispose() {
        this._toggle.removeEventListener('click', this._onToggleClick);
        if (this._doc && typeof this._doc.removeEventListener === 'function') {
            this._doc.removeEventListener('keydown', this._onKeyDown);
        }
    }

    /** @returns {boolean} Current collapsed state. */
    get collapsed() {
        return this._collapsed;
    }

    /** Flip the collapsed state and update all ARIA mirrors. */
    toggle() {
        this.setCollapsed(!this._collapsed);
    }

    /** Explicit setter (used by programmatic callers and the click handler). */
    setCollapsed(value) {
        const next = Boolean(value);
        if (next === this._collapsed) return;
        this._collapsed = next;
        this._applyState();
    }

    // ──────────────────────────── Internals ────────────────────────────────

    /** @private */
    _handleKeyDown(ev) {
        if (!isCollapseShortcut(ev)) return;
        if (isTextInput(ev.target)) return;
        if (typeof ev.preventDefault === 'function') ev.preventDefault();
        this.toggle();
    }

    /** @private */
    _applyState() {
        const collapsed = this._collapsed;
        this._root.setAttribute('data-panels-collapsed', String(collapsed));
        this._toggle.setAttribute('aria-expanded', String(!collapsed));

        if (this._scroller) {
            if (collapsed) {
                this._scroller.setAttribute('aria-hidden', 'true');
                this._scroller.setAttribute('inert', '');
            } else {
                this._scroller.removeAttribute('aria-hidden');
                this._scroller.removeAttribute('inert');
            }
        }

        // Focus management: if we just collapsed while focus was inside the
        // now-hidden region, move it back to the visible toggle button so
        // screen readers don't lose context.
        if (collapsed && this._scroller && this._doc && this._doc.activeElement
            && typeof this._scroller.contains === 'function'
            && this._scroller.contains(this._doc.activeElement)
            && typeof this._toggle.focus === 'function') {
            try { this._toggle.focus(); } catch { /* no-op */ }
        }
    }
}

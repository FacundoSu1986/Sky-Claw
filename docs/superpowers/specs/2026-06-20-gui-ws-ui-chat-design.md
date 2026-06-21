# Design — GUI ↔ daemon `/ws/ui` chat handler (Q&A)

## Context

The packaged GUI logs a reconnect loop of `GET /ws/ui 404`. The NiceGUI client
(`gui/agent_communication.py::AgentCommunicationClient`) connects to
`ws://localhost:8765/ws/ui` and sends `command/chat` frames, but **no server
serves `/ws/ui`**: the intended server (`comms/ws_daemon.py::UIBroadcastServer`)
is dead code (never started), and the only registered WS route is the Operations
Hub at `/api/status` (different protocol + close codes). The naive remap of
`/ws/ui` → the ops-hub handler was tried in **PR #195 and rejected** — it makes
the chat UX *worse* (the spinner hangs forever waiting for an `LLM_RESPONSE` the
ops-hub never sends). So the 404 is an **unfinished feature**, not a routing bug.

This spec builds the smallest version that makes GUI chat work: a **command-aware
`/ws/ui` handler that answers chat with the LLM** (reusing the existing
`POST /api/chat` logic). Agentic tool execution, progress streaming, and event
broadcasts are explicitly **out of scope** (a future, separately-designed phase).

**Outcome:** typing in the GUI chat returns an LLM response instead of
"⚠️ Daemon offline".

## Goal / non-goals

- **Goal:** GUI sends `{"type":"command","command":"chat","payload":{"text":…}}`
  over `/ws/ui`; the daemon replies `{"type":"response","payload":{"response":…}}`;
  the GUI renders it via its existing `LLM_RESPONSE` path.
- **Non-goals (YAGNI):** routing to the `SupervisorAgent`/tool execution, HITL in
  the GUI, progress/`broadcast`/`agent_result` frames, multi-turn streaming.

## Design (approach B — handler on `WebApp`)

A focused chat-WS handler mounted on the **same aiohttp app (`:8765`)** that already
serves `POST /api/chat` and the ops-hub `/api/status`. It reuses the `WebApp`'s
existing `self._router` (LLMRouter), `self._session`, `self._chat_id`, and
`self._auth_manager`. The ops-hub handler and `/api/status` are left untouched —
the chat protocol stays cleanly separate (the #195 conflation is avoided).

**Route registration:** in `WebApp.create_app`, add
`app.router.add_get("/ws/ui", self._handle_ws_ui)` **unconditionally** — exactly
like `/api/chat`, which is always registered and returns a graceful error when
the router is not yet configured (GUI wizard incomplete). Auth/router presence is
handled inside the handler, not at registration time.

**`_handle_ws_ui(request)` flow:**
1. **Prepare** the `WebSocketResponse` (`ws = web.WebSocketResponse()`; `await
   ws.prepare(request)`).
2. **Auth handshake.** Read `X-Auth-Token` from `request.headers` and validate via
   `AuthTokenManager`. On failure → `await ws.close(code=4001)` and return `ws`.
   **Close code MUST be 4001** — the client's `_AUTH_REJECTION_CLOSE_CODES = {4001}`;
   the ops-hub's `1008` would not be recognized as an auth reject. (Reuse the
   ops-hub's token-read logic; only the close code differs.) Then loop over incoming
   text frames, parsing JSON.
3. For `{"type":"command","command":"chat","payload":{"text": <msg>}}`:
   - `response = await self._router.chat(msg, self._session, chat_id=self._chat_id)`
     (identical call to `_handle_chat`).
   - `await ws.send_json({"type":"response","payload":{"response": response}})`.
4. **Other commands / unknown types:** ignore gracefully (no error frame, no close)
   — forward-compatible with a future agentic phase.
5. **Closed/cancelled:** exit the loop cleanly; return the `ws`.

**Error handling (never crash the connection):**
- Malformed JSON or missing `payload.text` → reply
  `{"type":"response","payload":{"response":"⚠️ Invalid chat frame."}}`.
- `self._router.chat(...)` raises → reply
  `{"type":"response","payload":{"response":"⚠️ <short error>"}}` (logged at ERROR;
  secret-redaction already applies via logging config).

## Data flow

```
GUI ChatController.process_user_message(text)
  → AgentCommunicationClient.send_chat_message(text)
     → ws send {"type":"command","command":"chat","payload":{"text":text}}
  daemon WebApp._handle_ws_ui:
     auth(X-Auth-Token) -> router.chat(text, session, chat_id) -> response str
     → ws send {"type":"response","payload":{"response":response}}
  GUI _handle_daemon_message: type=="response" → publish LLM_RESPONSE(payload)
     → ChatController.handle_llm_response → render in AppState, stop is_thinking
```

## Testing & verification (the gap that let PR #195 ship green-but-wrong)

PR #195 was green because **nothing tested the GUI↔daemon round-trip**. This spec
mandates that coverage:

1. **WS round-trip test (new):** spin up the `WebApp` aiohttp app with a stub/mock
   router whose `.chat()` returns a known string; connect a real WS client to
   `/ws/ui` with a valid token; send a `command/chat` frame; assert the reply is
   `{"type":"response","payload":{"response": <known string>}}`.
2. **Auth-reject test (new):** connect with a missing/invalid token; assert the
   server closes with **code 4001** (not 1008).
3. **Error test:** a router that raises → assert a `response` frame with the `⚠️`
   text, connection stays open.
4. **Manual verification:** rebuild the exe (or run gui mode), open the GUI, send a
   chat message, confirm an LLM reply renders (not "Daemon offline") and the
   `/ws/ui 404` reconnect spam is gone.

## Files touched

- `sky_claw/antigravity/web/app.py` — add `_handle_ws_ui` + route registration;
  reuse existing `_router`/`_session`/`_chat_id`/`_auth_manager` and the ops-hub's
  token-read helper (extract a tiny shared `_read_auth_token(request)` if cleaner).
- `tests/test_ws_ui_chat.py` (new) — round-trip + auth-reject + error tests.
- (No change to `operations_hub_ws.py` / `/api/status`, the GUI client, or the
  frame protocol — the client already speaks this protocol.)

## Release

Ships as a follow-up after v0.2.2 (likely **0.2.3**), via its own PR. `UIBroadcastServer`
dead-code removal is a separate optional cleanup, not required here.

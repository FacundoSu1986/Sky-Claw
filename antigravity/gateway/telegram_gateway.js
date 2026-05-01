"use strict";

/**
 * TELEGRAM WEBSOCKET GATEWAY (STANDARD 2026)
 * Implements a secure, stateless bridge between Telegram Bot API
 * and the Sky-Claw Python daemon.
 */

const { Bot } = require("grammy");
const { WebSocketServer } = require("ws");
const crypto = require("crypto");
require("dotenv").config();

// Configuration
const TELEGRAM_BOT_TOKEN = process.env.TELEGRAM_BOT_TOKEN;
const ALLOWED_USER_ID = parseInt(process.env.ALLOWED_USER_ID, 10);
const WS_PORT = 8080;

if (!TELEGRAM_BOT_TOKEN || isNaN(ALLOWED_USER_ID)) {
    console.error("CRITICAL ERROR: Please provide TELEGRAM_BOT_TOKEN and ALLOWED_USER_ID in .env");
    process.exit(1);
}

// SEC-007 FIX: Authentication configuration for WebSocket
if (!process.env.WS_AUTH_TOKEN) {
    console.error("[FATAL] WS_AUTH_TOKEN is not set. Cannot start telegram gateway without a shared secret.");
    process.exit(1);
}
const AUTH_TOKEN = process.env.WS_AUTH_TOKEN;
const AUTH_TIMEOUT_MS = 5000;

// SEC-007 FIX: Pre-calculate buffer for timing-safe comparison
const EXPECTED_TOKEN_BUFFER = Buffer.from(AUTH_TOKEN, 'utf8');

/**
 * SEC-007 FIX: Timing-safe token comparison
 * Uses crypto.timingSafeEqual() for constant-time comparison to prevent timing attacks.
 * @param {string} inputToken - The token to validate
 * @returns {boolean} - True if tokens match, false otherwise
 */
function timingSafeTokenCompare(inputToken) {
    try {
        const inputBuffer = Buffer.from(inputToken, 'utf8');

        // Verify length first to avoid crash
        if (inputBuffer.length !== EXPECTED_TOKEN_BUFFER.length) {
            return false;
        }

        return crypto.timingSafeEqual(EXPECTED_TOKEN_BUFFER, inputBuffer);
    } catch (err) {
        console.error('[GW] Token comparison error:', err.message);
        return false;
    }
}

// SEC-007 FIX: WebSocket Server with localhost-only verification and authentication
const wss = new WebSocketServer({
    port: WS_PORT,
    maxPayload: 1 * 1024 * 1024,
    verifyClient: (info, callback) => {
        // SEC-007 FIX: Only allow connections from localhost
        const remoteAddr = info.req.socket.remoteAddress;
        const isLocal = remoteAddr === "127.0.0.1" ||
            remoteAddr === "::1" ||
            remoteAddr === "::ffff:127.0.0.1";

        if (!isLocal) {
            console.warn(`[GW] SEC-007: Conexión rechazada desde ${remoteAddr} - Solo localhost permitido`);
            callback(false, 403, "Forbidden: Local connections only");
            return;
        }
        callback(true);
    }
});
let daemonSocket = null;

wss.on("connection", (ws, req) => {
    // Audit log (minimal)
    const remote = req.socket.remoteAddress;
    console.log(`[GW] Daemon connected from ${remote}`);

    // SEC-007 FIX: Authentication state tracking
    let isAuthenticated = false;
    let authTimeout = setTimeout(() => {
        if (!isAuthenticated) {
            console.warn(`[GW] SEC-007: Connection timeout - not authenticated`);
            ws.close(4001, "Authentication timeout");
        }
    }, AUTH_TIMEOUT_MS);

    ws.on("message", async (data) => {
        try {
            const message = JSON.parse(data);

            // SEC-007 FIX: Handle authentication first
            if (message.type === "auth") {
                if (timingSafeTokenCompare(message.token)) {
                    isAuthenticated = true;
                    clearTimeout(authTimeout);
                    ws.send(JSON.stringify({ type: "auth_success" }));
                    console.log(`[GW] SEC-007: Daemon authenticated successfully`);
                    // Si ya había un daemon registrado y vivo, cerrarlo grácilmente
                    // antes de reemplazarlo. Sin esto, una reconexión rápida del
                    // daemon dejaba el socket anterior huérfano (memory leak +
                    // mensajes potencialmente entregados al destinatario incorrecto).
                    if (daemonSocket && daemonSocket !== ws && daemonSocket.readyState === 1) {
                        try {
                            daemonSocket.close(4000, "Replaced by new daemon connection");
                        } catch (closeErr) {
                            console.warn("[GW] Error cerrando daemon previo:", closeErr.message);
                        }
                    }
                    daemonSocket = ws;
                    return;
                } else {
                    console.log(JSON.stringify({
                        event: "auth_failure",
                        timestamp: new Date().toISOString(),
                        source: "telegram_gateway",
                        remote: ws._socket?.remoteAddress || "unknown",
                        reason: "invalid_token"
                    }));
                    ws.close(4002, "Invalid token");
                    return;
                }
            }

            // SEC-007 FIX: Reject messages if not authenticated
            if (!isAuthenticated) {
                console.warn(`[GW] SEC-007: Message rejected - not authenticated`);
                ws.send(JSON.stringify({
                    type: "error",
                    error: "Authentication required"
                }));
                return;
            }

            // NOTE: scrape_nexus is intentionally NOT handled here.
            // All web-scraping logic belongs in the Python backend (sky_claw/scraper/).
            // This gateway is a stateless message bridge only.

            if (message.type === "response" || message.type === "hitl_request") {
                const text = message.payload?.text || message.data?.reason || "Mensaje del sistema recibido.";
                await bot.api.sendMessage(ALLOWED_USER_ID, text);
                console.log(`[GW] Relayed ${message.type} to user ${ALLOWED_USER_ID}`);
            }
        } catch (err) {
            console.error(`[GW] Error processing daemon message: ${err.message}`);
        }
    });

    ws.on("close", () => {
        console.log("[GW] Daemon disconnected");
        // Solo limpiar la referencia global si este ws es el daemon activo;
        // de lo contrario podría borrar el socket recién registrado por una
        // reconexión que provocó el close de uno anterior.
        if (daemonSocket === ws) {
            daemonSocket = null;
        }
    });

    ws.on("error", (err) => {
        console.error(`[GW] WebSocket Error: ${err.message}`);
    });
});

console.log(`[GW] WebSocket Server listening on port ${WS_PORT}`);

// 2. Telegram Perimeter Layer (Stateless)
const bot = new Bot(TELEGRAM_BOT_TOKEN);

// GTG-03: Token bucket rate limiter per user_id (5 msgs/min, burst 10)
const userBuckets = new Map();
const RATE_LIMIT_TOKENS = 5;
const RATE_LIMIT_BURST = 10;
const RATE_LIMIT_WINDOW_MS = 60_000;

function checkRateLimit(userId) {
    const now = Date.now();
    let bucket = userBuckets.get(userId);
    if (!bucket) {
        bucket = { tokens: RATE_LIMIT_TOKENS, last: now };
        userBuckets.set(userId, bucket);
    }
    // Refill tokens based on elapsed time
    const elapsed = now - bucket.last;
    bucket.tokens = Math.min(RATE_LIMIT_BURST, bucket.tokens + (elapsed / RATE_LIMIT_WINDOW_MS) * RATE_LIMIT_TOKENS);
    bucket.last = now;
    if (bucket.tokens < 1) return false;
    bucket.tokens--;
    return true;
}

bot.on("message:text", async (ctx) => {
    const userId = ctx.from.id;

    // Zero Trust validation
    if (userId !== ALLOWED_USER_ID) {
        // Drop silently to prevent log spam/probing
        return;
    }

    // GTG-03: Rate limit check
    if (!checkRateLimit(userId)) {
        console.warn(`[GW] Rate limit exceeded for user ${userId} — message dropped`);
        await ctx.reply("SISTEMA: Rate limit alcanzado. Espera unos segundos.")
            .catch(err => console.error("[GW] Reply failed:", err.message));
        return;
    }

    const text = ctx.message.text;
    const msgUuid = crypto.randomUUID();

    // Protocol packaging (Strict JSON Schema)
    const payload = {
        id: msgUuid,
        type: "command",
        action: "raw_text",
        payload: {
            text: text
        },
        metadata: {
            user_id: userId,
            timestamp: Date.now()
        }
    };

    if (daemonSocket && daemonSocket.readyState === 1) { // 1 is OPEN
        daemonSocket.send(JSON.stringify(payload));
        console.log(`[GW] Dispatched msg ${msgUuid} to daemon`);
    } else {
        console.error(`[GW] No daemon connected. Message dropped.`);
        await ctx.reply("SISTEMA: Conexión con el núcleo Python no establecida.")
            .catch(err => console.error("[GW] Reply failed:", err.message));
    }
});

// Error handling
bot.catch((err) => {
    console.error(`[GW] Grammy Error: ${err.message}`);
});

// Start Gateway
bot.start();
console.log("[GW] Telegram Bot Gateway started. Silent status ACTIVE.");

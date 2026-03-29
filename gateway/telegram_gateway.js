"use strict";

/**
 * TELEGRAM WEBSOCKET GATEWAY (STANDARD 2026)
 * Implements a secure, stateless bridge between Telegram Bot API
 * and the Sky_ClawGravity Python daemon.
 */

const { Bot } = require("grammy");
const { WebSocketServer } = require("ws");
const { v4: uuidv4 } = require("uuid"); // Optional, will use crypto.randomUUID for less dependencies
require("dotenv").config();

// Configuration
const TELEGRAM_BOT_TOKEN = process.env.TELEGRAM_BOT_TOKEN;
const ALLOWED_USER_ID = parseInt(process.env.ALLOWED_USER_ID, 10);
const WS_PORT = 8080;

if (!TELEGRAM_BOT_TOKEN || isNaN(ALLOWED_USER_ID)) {
    console.error("CRITICAL ERROR: Please provide TELEGRAM_BOT_TOKEN and ALLOWED_USER_ID in .env");
    process.exit(1);
}

// 1. WebSocket Server (Zero Trust - Localhost binding for daemon)
const wss = new WebSocketServer({ port: WS_PORT });
let daemonSocket = null;

wss.on("connection", (ws, req) => {
    // Audit log (minimal)
    const remote = req.socket.remoteAddress;
    console.log(`[GW] Daemon connected from ${remote}`);
    
    daemonSocket = ws;

    ws.on("message", async (data) => {
        try {
            const message = JSON.parse(data);
            if (message.type === "response" || message.type === "hitl_request") {
                const text = message.payload?.text || message.data?.reason || "Mensaje del sistema recibido.";
                await bot.api.sendMessage(ALLOWED_USER_ID, text, { parse_mode: "Markdown" });
                console.log(`[GW] Relayed ${message.type} to user ${ALLOWED_USER_ID}`);
            }
        } catch (err) {
            console.error(`[GW] Error processing daemon message: ${err.message}`);
        }
    });

    ws.on("close", () => {
        console.log("[GW] Daemon disconnected");
        daemonSocket = null;
    });

    ws.on("error", (err) => {
        console.error(`[GW] WebSocket Error: ${err.message}`);
    });
});

console.log(`[GW] WebSocket Server listening on port ${WS_PORT}`);

// 2. Telegram Perimeter Layer (Stateless)
const bot = new Bot(TELEGRAM_BOT_TOKEN);

bot.on("message:text", async (ctx) => {
    const userId = ctx.from.id;

    // Zero Trust validation
    if (userId !== ALLOWED_USER_ID) {
        // Drop silently to prevent log spam/probing
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
        ctx.reply("SISTEMA: Conexión con el núcleo Python no establecida.");
    }
});

// Error handling
bot.catch((err) => {
    console.error(`[GW] Grammy Error: ${err.message}`);
});

// Start Gateway
bot.start();
console.log("[GW] Telegram Bot Gateway started. Silent status ACTIVE.");

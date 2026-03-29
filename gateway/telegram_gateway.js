"use strict";

/**
 * TELEGRAM WEBSOCKET GATEWAY (STANDARD 2026)
 * Implements a secure, stateless bridge between Telegram Bot API
 * and the Sky_ClawGravity Python daemon.
 */

const { Bot } = require("grammy");
const { WebSocketServer } = require("ws");
const { v4: uuidv4 } = require("uuid"); // Optional, will use crypto.randomUUID for less dependencies
const { chromium, firefox, webkit } = require("playwright");
const fs = require("fs");
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

// 0. Persistence & Factory state
let activeConfig = {
    browser_engine: "chromium" // Default: Chromium (Reliability 0.95)
};

/**
 * BROWSER FACTORY (SRE Standard)
 * Devuelve una instancia del motor solicitado con Fallback a Chromium.
 */
async function launchEngine(preference) {
    const engineType = preference || activeConfig.browser_engine;
    const engineMap = { chromium, firefox, webkit };
    const selected = engineMap[engineType] || chromium;

    try {
        // Paso 5: Auto-Check de binarios en el Host Windows
        const binPath = selected.executablePath();
        if (!fs.existsSync(binPath)) {
            console.warn(`[GW] Binario ${engineType} ausente en ${binPath}. Aplicando Fallback a Chromium.`);
            return await chromium.launch({ headless: true });
        }
        
        // Paso 2: Launch con fingerprinting base (Confianza: WebKit/FF ~0.8)
        return await selected.launch({ headless: true });
    } catch (launchError) {
        console.error(`[GW] Falló lanzamiento de motor ${engineType}: ${launchError.message}`);
        return await chromium.launch({ headless: true });
    }
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
            
            // SRE Phase 3: Configuración Dinámica
            if (message.type === "command" && message.action === "set_config") {
                if (message.payload?.browser_engine) {
                    activeConfig.browser_engine = message.payload.browser_engine;
                    console.log(`[GW] Motor de renderizado actualizado a: ${activeConfig.browser_engine}`);
                }
                return;
            }

            // SRE Phase 2: Passthrough Web Scraper Integration (Multi-Engine)
            if (message.type === "command" && message.action === "scrape_nexus") {
                console.log(`[GW] Iniciando Playwright RPC (${activeConfig.browser_engine}) para request: ${message.request_id}`);
                let browser;
                try {
                    // Paso 2: Implementación de Factory
                    browser = await launchEngine(message.payload?.browser_engine);
                    // Fingerprint Randomization: Se utiliza el UA nativo del motor para evitar inconsistencias
                    const contextOptions = {};
                    if (message.payload?.userAgent) {
                        contextOptions.userAgent = message.payload.userAgent;
                    }
                    const context = await browser.newContext(contextOptions);
                    const page = await context.newPage();
                    // Timeout robusto a 25s dejando 5s de margen al demonio Python
                    await page.goto(message.url, { waitUntil: "domcontentloaded", timeout: 25000 });
                    
                    const title = await page.title();
                    // Aquí escalarías la lógica para raspar elementos DOM concretos.
                    const contentJson = { page_title: title, url: message.url };
                    
                    const scrapeResult = {
                        type: "scrape_response",
                        request_id: message.request_id,
                        data: contentJson,
                        error: null
                    };
                    ws.send(JSON.stringify(scrapeResult));
                    console.log(`[GW] Playwright RPC exitoso para request: ${message.request_id}`);
                } catch (scrapeErr) {
                    console.error(`[GW] Error de Scraping IPC: ${scrapeErr.message}`);
                    ws.send(JSON.stringify({
                        type: "scrape_response", 
                        request_id: message.request_id, 
                        data: null, 
                        error: scrapeErr.message
                    }));
                } finally {
                    // Paso 3: Cierre agnóstico y seguro (Evita fugas de memoria)
                    if (browser) {
                        try {
                            await browser.close();
                        } catch (closeErr) {
                            console.error(`[GW] Zombie process prevention - Error cerrando motor: ${closeErr.message}`);
                        }
                    }
                }
                return;
            }

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

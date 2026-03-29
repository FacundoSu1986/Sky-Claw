/**
 * scripts/chaos_test.js
 * Prueba de Resiliencia y Chaos Engineering para el Ecosistema Sky-Claw.
 * 
 * Escenario: Carga constante de 10 req/s con inyección de fallo (OOM simulado).
 */

const { exec } = require('child_process');
const WebSocket = require('ws');
const path = require('path');
const fs = require('fs');

const UI_URL = 'ws://127.0.0.1:18790';
const PID_FILE = path.join(__dirname, '..', '.run', 'skyclaw.pid');
const RESTART_SCRIPT = path.join(__dirname, 'restart_agent.ps1');

console.log('--- Iniciando Prueba de Chaos Engineering (Resiliencia de Gateway) ---');

const ws = new WebSocket(UI_URL);
let messageCount = 0;
let failInjected = false;
let startTime = Date.now();

ws.on('open', () => {
    console.log('[CLIENT] Conectado al Gateway (Puerto UI: 18790)');
    
    // 1. Simulación de Carga: 10 req/sec
    const loadGenerator = setInterval(() => {
        const elapsed = (Date.now() - startTime) / 1000;
        messageCount++;
        
        const payload = {
            id: messageCount,
            type: 'QUERY',
            content: `Simulated request #${messageCount} at T+${elapsed.toFixed(1)}s`
        };
        
        ws.send(JSON.stringify(payload));

        if (messageCount % 20 === 0) {
            console.log(`[LOAD] Generados ${messageCount} mensajes...`);
        }

        // 2. Inyección de Fallo: Crash a los 5 segundos (aprox 50 mensajes)
        if (elapsed > 5 && !failInjected) {
            failInjected = true;
            console.log('\n[CHAOS] !!! INYECTANDO FALLO: Matando Daemon de Python (SIMULATED OOM) !!!');
            
            if (fs.existsSync(PID_FILE)) {
                const pid = fs.readFileSync(PID_FILE, 'utf-8').trim();
                exec(`powershell -Command "Stop-Process -Id ${pid} -Force"`, (err) => {
                    if (err) console.error('[CHAOS] Error al matar el proceso:', err.message);
                    else console.log(`[CHAOS] Daemon (PID ${pid}) terminado abruptamente.`);
                });
            } else {
                console.error('[CHAOS] Error: No se encontró .run/skyclaw.pid');
            }
        }

        // Detener después de 20 segundos de observación
        if (elapsed > 20) {
            clearInterval(loadGenerator);
            console.log('\n--- Prueba de Carga Finalizada ---');
            console.log(`Mensajes Totales: ${messageCount}`);
            process.exit(0);
        }
    }, 100); // 100ms interval = 10 req/sec
});

ws.on('message', (data) => {
    const msg = JSON.parse(data);
    
    // 3. Validación de Retención y Recuperación
    if (msg.type === 'STATUS') {
        if (msg.content.includes('[BUFFERING]')) {
            console.log(`[RESILIENCE] Detectado estado de BUFFERING: ${msg.content}`);
        } else if (msg.content.includes('[READY]')) {
            console.log(`[RESILIENCE] !!! RECUPERACIÓN DETECTADA: ${msg.content} !!!`);
            console.log('[RESILIENCE] El Gateway está drenando la cola hacia el nuevo Daemon.');
        }
    } else {
        // Respuestas del agente
        // console.log(`[AGENT] Respuesta: ${msg.content || JSON.stringify(msg)}`);
    }
});

ws.on('close', () => {
    console.error('[CRITICAL] El Gateway cerró la conexión UI. PRUEBA FALLIDA (Resiliencia de Socket)');
    process.exit(1);
});

ws.on('error', (err) => {
    console.error('[ERROR] Error en el cliente de prueba:', err.message);
});

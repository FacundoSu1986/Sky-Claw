const assert = require('node:assert/strict');
const { on, once } = require('node:events');
const fs = require('node:fs');
const http = require('node:http');
const { createRequire } = require('node:module');
const path = require('node:path');
const { test } = require('node:test');
const vm = require('node:vm');
const { WebSocket, WebSocketServer } = require('ws');

const TOKEN = 'token-falso-de-test';

async function arrancarGateway(t, signal) {
    const entrada = path.resolve(__dirname, '../server.js');
    const requireGateway = createRequire(entrada);
    const sockets = [];
    let salud;
    const silenciar = () => {};

    // Ejecuta el server.js completo con sockets reales y puertos efímeros.
    // El contexto separa los listeners globales y el .env del operador.
    const modulos = {
        dotenv: { config: silenciar },
        ws: {
            WebSocketServer: class extends WebSocketServer {
                constructor(opciones) {
                    super({ ...opciones, port: 0 });
                    sockets.push(this);
                }
            },
        },
        http: {
            createServer(handler) {
                salud = http.createServer(handler);
                const listen = salud.listen.bind(salud);
                salud.listen = (_port, host, callback) => listen(0, host, callback);
                return salud;
            },
        },
    };
    t.after(async () => {
        for (const servidor of sockets) {
            for (const cliente of servidor.clients) cliente.terminate();
        }
        await Promise.all([...sockets, salud].filter(Boolean).map(
            servidor => new Promise(resolve => servidor.close(resolve)),
        ));
    });
    vm.runInNewContext(fs.readFileSync(entrada, 'utf8'), {
        require: nombre => modulos[nombre] ?? requireGateway(nombre),
        __dirname: path.dirname(entrada),
        Buffer,
        console: { log: silenciar, warn: silenciar, error: silenciar },
        process: {
            env: { WS_AUTH_TOKEN: TOKEN, SSL_KEY_PATH: entrada + '/ausente.key', SSL_CERT_PATH: entrada + '/ausente.crt' },
            on: silenciar,
            uptime: process.uptime,
        },
        setTimeout: (...args) => setTimeout(...args).unref(),
        clearTimeout,
    }, { filename: entrada });
    await Promise.all([...sockets, salud].map(servidor => once(servidor, 'listening', { signal })));
    return { agente: sockets[0], interfaz: sockets[1], salud };
}

async function esperarMensaje(cliente, coincide, signal) {
    for await (const [dato] of on(cliente, 'message', { signal })) {
        const mensaje = JSON.parse(dato);
        if (coincide(mensaje)) return mensaje;
    }
}

async function conectar(t, servidor, signal) {
    const cliente = new WebSocket(`ws://127.0.0.1:${servidor.address().port}`);
    t.after(() => cliente.terminate());
    await once(cliente, 'open', { signal });
    const autenticado = esperarMensaje(cliente, mensaje => mensaje.type === 'auth_ok', signal);
    cliente.send(JSON.stringify({ type: 'auth', token: TOKEN }));
    await autenticado;
    return cliente;
}

test('el cierre del agente reemplazado conserva el agente activo y sus comandos', { timeout: 8000 }, async (t) => {
    const signal = AbortSignal.timeout(5000);
    const { agente, interfaz, salud } = await arrancarGateway(t, signal);
    const ui = await conectar(t, interfaz, signal);
    const avisos = [];
    ui.on('message', dato => avisos.push(JSON.parse(dato)));
    await conectar(t, agente, signal);

    // Esperar el close en el socket DEL SERVIDOR garantiza que el handler de
    // producción ya corrió antes de consultar salud o enviar el comando.
    const socketAnterior = agente.clients.values().next().value;
    const cierreAnterior = once(socketAnterior, 'close', { signal });
    const nuevo = await conectar(t, agente, signal);
    const [codigo] = await cierreAnterior;
    assert.equal(codigo, 4000);

    const leerSalud = async () => {
        const respuesta = await fetch(`http://127.0.0.1:${salud.address().port}/health`, { signal });
        assert.equal(respuesta.status, 200);
        return respuesta.json();
    };
    assert.equal((await leerSalud()).agent_connected, true);

    const comando = { type: 'COMMAND', content: 'prueba de reemplazo' };
    const recibido = esperarMensaje(nuevo, mensaje => mensaje.type === comando.type, signal);
    ui.send(JSON.stringify(comando));
    assert.deepEqual(await recibido, comando);

    const respuesta = { type: 'RESPONSE', content: 'agente nuevo' };
    const retransmitida = esperarMensaje(ui, mensaje => mensaje.type === respuesta.type, signal);
    nuevo.send(JSON.stringify(respuesta));
    assert.deepEqual(await retransmitida, respuesta);
    // La respuesta viaja por el mismo WebSocket que el aviso de desconexión.
    assert.ok(!avisos.some(mensaje => mensaje.content?.startsWith('[BUFFERING]')));

    const offline = esperarMensaje(ui, mensaje => mensaje.content?.startsWith('[BUFFERING]'), signal);
    nuevo.close();
    await offline;
    assert.equal((await leerSalud()).agent_connected, false);
});

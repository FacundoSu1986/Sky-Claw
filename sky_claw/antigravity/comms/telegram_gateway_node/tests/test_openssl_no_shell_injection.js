/**
 * H-3: la generación de certificado TLS auto-firmado NO debe construir un
 * string de shell interpolando keyPath/certPath (derivados de process.env.HOME).
 *
 * Con execFileSync + array de argumentos no interviene ningún shell, así que un
 * HOME con metacaracteres (';', backticks, '$()') se pasa como argv literal y no
 * se ejecuta. Este test verifica dos cosas:
 *   1. El helper pasa los paths como elementos literales del array de argv.
 *   2. server.js usa execFileSync (no execSync con openssl interpolado).
 */

const fs = require('fs');
const path = require('path');
const assert = require('assert');

// Réplica del helper bajo prueba (mismo patrón que test_timing_safe_equal.js),
// con runner inyectable para capturar los argumentos.
function generateSelfSignedCert(keyPath, certPath, runner) {
    runner('openssl', [
        'req', '-x509', '-newkey', 'rsa:2048',
        '-keyout', keyPath,
        '-out', certPath,
        '-days', '365', '-nodes',
        '-subj', '/CN=localhost',
    ], { stdio: 'pipe' });
}

let passed = 0;
let failed = 0;

function check(desc, fn) {
    try {
        fn();
        passed++;
        console.log(`✅ PASS: ${desc}`);
    } catch (err) {
        failed++;
        console.error(`❌ FAIL: ${desc} — ${err.message}`);
    }
}

// 1. Un HOME malicioso llega como argv literal, sin ejecutarse.
check('paths con metacaracteres se pasan como argv literal', () => {
    const evilKey = '/home/user; touch /tmp/pwned/.sky_claw/certs/server.key';
    const evilCert = '/home/`whoami`/.sky_claw/certs/server.crt';
    let captured = null;
    generateSelfSignedCert(evilKey, evilCert, (cmd, args) => {
        captured = { cmd, args };
    });
    assert.strictEqual(captured.cmd, 'openssl', 'el comando debe ser openssl exacto');
    assert.ok(Array.isArray(captured.args), 'los argumentos deben ser un array');
    // keyPath/certPath aparecen como elementos completos, sin trocear por shell.
    assert.ok(captured.args.includes(evilKey), 'keyPath debe llegar literal en argv');
    assert.ok(captured.args.includes(evilCert), 'certPath debe llegar literal en argv');
    // Nunca debe existir un único string con el comando concatenado.
    assert.ok(
        !captured.args.some((a) => typeof a === 'string' && a.includes('openssl req')),
        'no debe haber un string de shell concatenado',
    );
});

// 2. Regresión sobre el archivo real.
check('server.js usa execFileSync y no execSync interpolado con openssl', () => {
    const src = fs.readFileSync(path.join(__dirname, '..', 'server.js'), 'utf8');
    assert.ok(src.includes('execFileSync'), 'server.js debe importar/usar execFileSync');
    assert.ok(
        !/execSync\(\s*`openssl/.test(src),
        'server.js NO debe usar execSync con un template de openssl interpolado',
    );
});

console.log(`\nResults: ${passed} passed, ${failed} failed`);
if (failed > 0) process.exit(1);

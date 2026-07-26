/**
 * Quick verification test for GWS-04:
 * timingSafeEqual must compare buffer lengths (UTF-8 bytes) not string lengths.
 */

const crypto = require('crypto');

function timingSafeEqual(a, b) {
    if (typeof a !== 'string' || typeof b !== 'string') {
        return false;
    }
    const bufA = Buffer.from(a, 'utf8');
    const bufB = Buffer.from(b, 'utf8');
    if (bufA.length !== bufB.length) {
        return false;
    }
    try {
        return crypto.timingSafeEqual(bufA, bufB);
    } catch {
        return false;
    }
}

// Test cases
const tests = [
    // Exact match
    { a: 'secret-token-123', b: 'secret-token-123', expected: true, desc: 'exact match' },
    // Mismatch
    { a: 'secret-token-123', b: 'secret-token-456', expected: false, desc: 'different tokens' },
    // UTF-8 multibyte exact match (the core fix)
    { a: 'tökén', b: 'tökén', expected: true, desc: 'UTF-8 multibyte exact match' },
    // UTF-8 multibyte mismatch (same JS string length, different bytes)
    { a: 'tökén', b: 'token', expected: false, desc: 'UTF-8 multibyte mismatch' },
    // Different byte lengths via multibyte chars
    { a: '日本語', b: '日本語', expected: true, desc: 'CJK exact match' },
    { a: '日本語', b: '日本语', expected: false, desc: 'CJK mismatch' },
];

let passed = 0;
let failed = 0;

for (const t of tests) {
    const result = timingSafeEqual(t.a, t.b);
    if (result === t.expected) {
        passed++;
        console.log(`✅ PASS: ${t.desc}`);
    } else {
        failed++;
        console.error(`❌ FAIL: ${t.desc} — expected ${t.expected}, got ${result}`);
    }
}

console.log(`\nResults: ${passed} passed, ${failed} failed`);
if (failed > 0) process.exit(1);

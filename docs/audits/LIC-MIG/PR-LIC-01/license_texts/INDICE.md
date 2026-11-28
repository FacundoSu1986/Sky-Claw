# Índice de textos de licencia (PR-LIC-01)

Generado: 2026-11-28T12:43:23+00:00

Base canónica: `spdx/license-list-data` (rama `main`, recuperada 2026-11-28,
SHA-256 del tarball `084868b7c2be0019f3a7fa1460a1e71a93f1236ee6545e657843f00bd1c13b4d`).

| Licencia | Componentes que la invocan | Texto en este directorio |
|---|---|---|
| 0BSD | numpy 2.4.6, rarfile 4.2 | `0BSD.txt` |
| Apache-2.0 | DOMPurify (vendorizado en nicegui/static/dompurify.mjs) 3.4.0 (según banner en el archivo), Material Icons / Material Symbols / Roboto (16 WOFF2 vendorizados dentro de NiceGUI) Material Icons 1.017 / Material Icons Outlined-Round-Sharp 2022-08-02 / Material Symbols 2.911 / Roboto 3.009, OpenSSL (embebido por la distribución Windows de CPython) 3.x (NO VERIFICADO), PyInstaller run-time hooks y módulos falsos (pyi_rth_*, _pyi_rth_utils) 6.22.3 (+ pyinstaller-hooks-contrib 2026.10), aiofiles 24.1.0, aiohttp 3.14.4 (+22 más) | `Apache-2.0.txt` |
| BSD-2-Clause | pycryptodomex 3.23.0, pygments 2.20.0, python-json-logger 3.3.0 | `BSD-2-Clause.txt` |
| BSD-3-Clause | backports-zstd 1.5.0, click 8.3.3, colorama 0.4.6, cryptography 50.0.0, grpcio 1.80.0, httpcore 1.0.9 (+17 más) | `BSD-3-Clause.txt` |
| CC0-1.0 | numpy 2.4.6 | `CC0-1.0.txt` |
| GPL-2.0-or-later | PyInstaller bootloader + loader de runtime 6.22.3 | `GPL-2.0-or-later.txt` |
| GPL-3.0-or-later | auto_parallax_regla.py (transcripción de la regla del plugin Auto Parallax) archivo único; upstream citado: rama main de doodlum/skyrim-auto-parallax descargada 2026-10-27, SHA-256 c70f6e21…88a3 | `GPL-3.0-or-later.txt` |
| HPND | pillow 13.0.0 | `HPND.txt` |
| LGPL-2.1-or-later | inflate64 1.0.4, multivolumefile 0.2.3, py7zr 1.1.3, pybcj 1.0.7, pyppmd 1.3.1 | `LGPL-2.1-or-later.txt` |
| MIT | Immutable.js (vendorizado: immutable.es.js) NO VERIFICADO, Quasar Framework (vendorizado: quasar.umd.js, quasar.*.css) NO VERIFICADO, Recursos y código propios (xEdit Pascal, shim C# de Synthesis, styles.css, security_policy.yaml) n/a, Tailwind CSS browser build + UnoCSS y presets (vendorizados) NO VERIFICADO, Vue.js (vendorizado: vue.esm-browser.js y .prod.js) NO VERIFICADO (banner no declara versión), aiohttp 3.14.4 (+40 más) | `MIT.txt` |
| MIT-0 | cffi 2.0.0, h11 0.16.0 | `MIT-0.txt` |
| MPL-2.0 | DOMPurify (vendorizado en nicegui/static/dompurify.mjs) 3.4.0 (según banner en el archivo), bidict 0.23.1, certifi 2026.10.19, grpcio 1.80.0 | `MPL-2.0.txt` |
| OFL-1.1 | Cinzel (WOFF2, subsets latin/latin-ext) Version 2.000, EB Garamond (WOFF2, normal/italic × latin/latin-ext) Version 1.001 (el upstream actual sirve 1.002 para Regular), Noto Sans Runic (WOFF2, 3 subsets) Version 2.002, Spline Sans Mono (WOFF2, latin/latin-ext) Version 1.004 (coincide con el upstream actual) | `OFL-1.1.txt` |
| PSF-2.0 | CPython (intérprete + stdlib congelados) 3.11.x (parche exacto NO VERIFICADO), aiohappyeyeballs 2.6.1, backports-zstd 1.5.0, defusedxml 0.7.1, typing-extensions 4.15.0 | `PSF-2.0.txt` |
| Unlicense | SQLite (módulo sqlite3 del stdlib) 3.x (NO VERIFICADO), pycryptodomex 3.23.0 | `Unlicense.txt` |
| Zlib | numpy 2.4.6, pillow 13.0.0 | `Zlib.txt` |

## Regla de precedencia

1. Si el artefacto exacto distribuido trae su propio texto, **se distribuye ese texto**
   (`evidence/pypi/<paquete>/…`), no la versión canónica: es la prueba de lo que se
   recibió y de lo que se redistribuye.
2. Si el artefacto no trae texto (certifi, avisos Apache de los recursos web de NiceGUI,
   fuentes, PyInstaller, CPython, OpenSSL), se usa el texto de la fuente primaria del
   componente, con URL y fecha registradas en `evidence/registry/`.
3. Ningún texto se transcribe de memoria.

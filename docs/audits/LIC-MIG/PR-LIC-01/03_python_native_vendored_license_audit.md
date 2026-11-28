# PR-LIC-01 — 03 · Auditoría de licencias: Python, nativo y vendorizado (LIC01-A / LIC01-B / LIC01-C)

**Base de datos:** `02_effective_component_license_matrix.csv` (124 filas).
**Método:** CLAIM → EXTRACT → DOUBT → RECONCILE, con evidencia primaria recuperada hoy y sellada.

---

## 1. Método aplicado (y por qué no se aceptó la autoridad de auditorías previas)

| Paso | Qué se hizo | Artefacto |
|---|---|---|
| CLAIM | se leyó lo que declara cada fuente: `License-Expression`, `License`, classifiers de PyPI, banners dentro de archivos vendorizados, `package.json` | `evidence/pypi/*/metadata.json` |
| EXTRACT | se descargó el **artefacto exacto** fijado por el lock y se extrajeron sus propios `LICENSE/COPYING/NOTICE`; para componentes no-PyPI, el archivo de licencia del repo en una **ref fija** | `evidence/pypi/<pkg>/`, `evidence/github/` |
| DOUBT | el texto se comparó contra la base canónica de SPDX midiendo **cobertura de shingles** (no razón global ni memoria del auditor) | `evidence/pypi/analisis_licencias.json` |
| RECONCILE | se concluyó licencia **sólo** con evidencia del texto; las discrepancias se registran como `EVIDENCE_CONFLICT` y las ausencias como `EVIDENCE_MISSING` | matriz, columnas `license_concluded` / `evidence_status` |

Resultado de integridad de la extracción:

- **94/94** artefactos con SHA-256 **idéntico al declarado por PyPI**.
- **94/94** artefactos con SHA-256 **presente en el lockfile** del repo (es decir: es exactamente el
  artefacto que el build instala, no "una versión parecida").
- **93/94** con al menos un archivo de licencia dentro del artefacto.
- **13/94** con **más de un** archivo de licencia (señal temprana de código de terceros vendorizado
  dentro del paquete contenedor — ver §3).
- **0** extracciones desde sdist: en todos los casos bastó el wheel, lo que evita cualquier
  diferencia entre "árbol de fuentes" y "lo que efectivamente se instala".

Ninguna conclusión de este documento usa classifiers de PyPI ni `License-Expression` como prueba.

## 2. Resultado LIC01-A — dependencias Python

**Universo:** 94 distribuciones en la unión de `requirements.lock` (perfil base, 92) y
`requirements-native-parallax.lock` (perfil native-parallax, 94). Las cifras históricas de "87/89"
son referencias: aquí el número sale del lock actual, no se fuerza a coincidir.

| Perfil | Distribuciones | Nota |
|---|---|---|
| `base` | 92 | perfil que construye `build.bat` / CI |
| `native-parallax` | 94 | añade `numpy 2.4.6` y `pillow 13.0.0` |
| unión | **94** | 92 en ambos perfiles + 2 sólo en native-parallax (`numpy 2.4.6`, `pillow 13.0.0`) |

**Redistribución efectiva (reconciliación, no lockfile):**

- 106 imports de primer nivel detectados por AST en `sky_claw/` + 69 `hiddenimports` del spec como
  semilla, cerrados transitivamente por `requires_dist`.
- **93/94 alcanzables**; **1 no alcanzable**: `sv-ttk` (declarada en `pyproject.toml`, importada por
  **ningún** módulo). Se registra como `NO_ALCANZABLE_POR_IMPORT` y **no** se borra de la matriz.
- Advertencia de método: "no alcanzable" **no** equivale a "no redistribuido" (un hook de PyInstaller
  puede introducirlo). La prueba definitiva es el Analysis TOC, **EVIDENCE_MISSING** en esta fase.

**Clasificación por licencia concluida** (filas, no "calidad"): MIT 41 · Apache-2.0 21 ·
BSD-3-Clause 19 · LGPL-2.1-or-later 5 · PSF-2.0 4 · MPL-2.0 2 · MIT-0 2 · BSD-2-Clause 2 ·
0BSD/CC0/Unlicense/Zlib/HPND 1 c/u · compuestas o de elección 9.

## 3. Casos que exigen corrección de expectativas (los "hallazgos incómodos")

### 3.1 Paquetes que **no** son una sola licencia

| Componente | Qué contiene realmente el artefacto | Consecuencia |
|---|---|---|
| `numpy 2.4.6` | `LICENSE.txt` de **45 KB agregado**: BSD-3 + 0BSD + MIT + Zlib + CC0-1.0 + Apache-2.0 + avisos vendorizados (libdivide, highway, x86-simd-sort, pocketfft, lapack_lite, svml, generadores) + mención `GPL-3.0-or-later WITH GCC-exception-3.1` (aviso de excepción de runtime de GCC) | hay que reproducir el archivo completo, no "la licencia de numpy" |
| `pillow 13.0.0` | `LICENSE` de **78 KB**: MIT-CMU/HPND + zlib + libjpeg-turbo + libimagequant + BSD-2/3 + MIT | ídem; el metadato dice `MIT-CMU` y eso es sólo la cabeza |
| `grpcio 1.80.0` | un solo `LICENSE` con **Apache-2.0 + BSD-3 (Google) + MPL-2.0 completo** | introduce obligación MPL (ver §5) |
| `aiohttp 3.14.4` | `LICENSE.txt` (Apache-2.0) **y** `dist-info/licenses/vendor/llhttp/LICENSE` (MIT, Fedor Indutny) | atribución separada por código vendorizado |
| `cryptography 50.0.0` | `LICENSE` de **197 bytes** (puntero) + `LICENSE.APACHE` + `LICENSE.BSD` | elección dual: hay que declarar cuál se invoca |
| `orjson 3.11.9` | `LICENSE-APACHE` + `LICENSE-MIT`; el metadato declara además `MPL-2.0` **cuyo texto no está** | contradicción registrada, no resuelta |
| `pycryptodomex 3.23.0` | `LICENSE.rst` con **Unlicense + BSD-2** según zona del árbol | no es "dominio público" a secas |
| `prometheus-client 0.25.0` | `LICENSE` (Apache-2.0) + `NOTICE` que declara empaquetar `decorator 4.0.10` (BSD-2) **sin su texto** | falta un aviso obligatorio |
| `python-json-logger 3.3.0` | `LICENSE` (BSD-2) + `NOTICE` que menciona `mkdocstrings-python` (ISC) | verificar si ese código viaja |

### 3.2 Metadato que contradice al texto

| Componente | Declarado | Texto del artefacto | Tratamiento |
|---|---|---|---|
| `cffi 2.0.0` | MIT | **MIT-0** — "MIT No Attribution", sin cláusula de atribución | `license_concluded = MIT-0`, `EVIDENCE_CONFLICT`; **no se puede exigir** una atribución que el texto no pide |
| `colorama`, `psutil`, `websockets`, `pycparser`, `starlette`, `uvicorn`, `tinycss2`, `httpcore`, `httpx`, `lxml`, `python-dotenv`, `pywin32-ctypes` | "BSD License" / BSD-3 | texto BSD-3 con redacción propia (cobertura 0,81–0,88 contra el canónico: la cláusula de endorsement cambia de sujeto) | concluida BSD-3-Clause; se distribuye **el texto del artefacto**, no el canónico |

### 3.3 Paquetes **sin** texto de licencia en ningún artefacto

| Componente | Situación | Estado |
|---|---|---|
| `webencodings 0.5.1` | wheel **y** sdist descargados con hash verificado: ninguno contiene `LICENSE`/`COPYING` | `EVIDENCE_MISSING` → el texto debe recuperarse del repo upstream y verificarse (PR-LIC-02) |
| `certifi 2026.10.19` | el único archivo (989 B) **nombra** MPL-2.0 y describe un `ca-bundle.crt` modificado, pero **no reproduce el texto** | `EVIDENCE_EXACT_ARTIFACT_TEXTO_INCOMPLETO` → incorporar MPL-2.0 íntegro |
| `docutils 0.22.4` | `COPYING.rst` describe régimen mixto (dominio público / BSD-2 / GPL-2 por partes) | `EVIDENCE_CONFLICT` → revisión jurídica |

## 4. Resultado LIC01-B — nativos y vendorizados

### 4.1 PyInstaller (la pregunta que había que responder con la fuente exacta, no de memoria)

Texto recuperado del **tag exacto** que fija el lock (`pyinstaller==6.22.3`), `COPYING.txt`:

- **Bootloader** y `PyInstaller/loader`: `GPL-2.0-or-later` **WITH Bootloader Exception**, que
  autoriza explícitamente "link or embed compiled bootloader and related files into combinations
  with other programs, and to distribute those combinations **without any restriction** coming from
  the use of those files".
- **Run-time hooks** (`PyInstaller/hooks/rthooks`): **Apache-2.0** (declarado en el mismo
  `COPYING.txt`).
- **Módulos adicionales de runtime** (`pyi_splash`, `_pyi_rth_utils`): **Apache-2.0**.
- `PyInstaller.isolated`: GPL-2.0 **o** MIT a elección (irrelevante aquí: no se congela).

Conclusión: el bootloader **no** impone GPL-3.0 al proyecto. Lo que sí exige es reproducir el aviso
del bootloader y la licencia Apache-2.0 de los rthooks. Esto es un **hecho verificado en la fuente
primaria del tag usado**, no una inferencia.

### 4.2 CPython y sus terceros embebidos

- CPython: `PSF-2.0` (texto del tag `v3.11.9`, commit `de54cf5be371`), más `Doc/license.rst` (53 KB)
  con los avisos de OpenSSL, SQLite, libffi, zlib, expat, bz2, lzma, mpdecimal.
- **Versión exacta: NO VERIFICADA.** `build_env.json` está `EVIDENCE_MISSING`; `ci.yml` fija
  `python-version: 3.11` (parche lo decide el runner). Sin Analysis TOC tampoco se puede afirmar qué
  DLL viaja dentro del onefile. Se registran 4 filas con
  `OBLIGACION_REGISTRADA_VERSION_SIN_VERIFICAR` y la pregunta abierta explícita.
- OpenSSL, SQLite, libffi: textos primarios recuperados (OpenSSL Apache-2.0 del tag
  `openssl-3.0.16`, libffi MIT de `v3.4.6`, SQLite dominio público **desde un espejo de GitHub**
  porque `sqlite.org` no es alcanzable — se declara).
- Tcl/Tk: no se incluyó fila de redistribución porque `tkinter` no se importa; si un hook lo metiera,
  aparecería en el inventario y habría que abrir la fila.

### 4.3 JavaScript vendorizado dentro de NiceGUI 3.12.0

Wheel `nicegui-3.12.0-py3-none-any.whl` (SHA-256 verificado contra PyPI) contiene **115 archivos
estáticos, 9.129.120 bytes**: 88 `.js`, 7 `.css`, 16 `.woff2`, 1 `.mjs`, 1 `.map`, 1 `.svg`,
1 `.ico`.

| Recurso | Licencia observable | Versión | Estado |
|---|---|---|---|
| `dompurify.mjs` | Apache-2.0 **OR** MPL-2.0 (banner) | 3.4.0 (en el banner) | `SUPPORTED_BY_METADATA`; falta el texto |
| `vue.esm-browser*.js` | MIT (banner) | no declarada | `SUPPORTED_BY_METADATA` |
| `quasar.umd*.js`, `quasar.*.css` | MIT (banner) | no declarada | `SUPPORTED_BY_METADATA` |
| `socket.io.min.js` | MIT (banner) | no declarada | `SUPPORTED_BY_METADATA` |
| `immutable.es.js` | MIT (banner) | no declarada | `SUPPORTED_BY_METADATA` |
| `sass.dart.js` (3,1 MB) | **sin banner**: ni licencia ni copyright | no declarada | `EVIDENCE_MISSING` |
| `tailwindcss.min.js`, `unocss/*.js` | sin banner; MIT por upstream | no declarada | `INFERRED` |
| `fonts/*.woff2` (16) | Apache-2.0 (copyright Google en el `name` table) | Material Icons 1.017 / Symbols 2.911 / Roboto 3.009 | `SUPPORTED_BY_METADATA` |

**Corrección de un supuesto habitual:** los archivos vendorizados **no** heredan la licencia del
paquete contenedor. NiceGUI es MIT; sus estáticos incluyen Apache-2.0, MIT y una parte sin
identificar. El inventario del ejecutable ya los clasifica como `THIRD_PARTY_VENDORED` con estado
`NO_VERIFICADO`: este informe los verifica **por primera vez**.

## 5. LIC01-H (avance) — inventario de copyleft débil hallado en LIC01-A

| Componente | Licencia | Archivos | `source_obligation` |
|---|---|---|---|
| `py7zr 1.1.3` | LGPL-2.1-or-later | `LICENSE` (texto base v2.1) | **SI** |
| `pybcj 1.0.7` | LGPL-2.1-or-later | `LICENSE` | **SI** |
| `pyppmd 1.3.1` | LGPL-2.1-or-later | `LICENSE` | **SI** |
| `inflate64 1.0.4` | LGPL-2.1-or-later | `LICENSE` | **SI** |
| `multivolumefile 0.2.3` | LGPL-2.1-or-later | `LICENSE` | **SI** |
| `bidict 0.23.1` | MPL-2.0 | `LICENSE` (cobertura 0,994) | **SI** |
| `certifi 2026.10.19` | MPL-2.0 | sin texto | **SI** |
| `grpcio 1.80.0` | MPL-2.0 (parte del agregado) | `LICENSE` | **SI** |
| `orjson 3.11.9` | MPL-2.0 **declarada**, texto ausente | — | **SI (sin evidencia)** |
| DOMPurify | MPL-2.0 como alternativa | — | **NO** si se elige Apache-2.0 (decisión a declarar) |

Todo esto es **atribución y conservación de textos**, no resolución de mecanismo. El mecanismo
(disponibilidad de fuente correspondiente, relinking/reemplazo en un onefile de PyInstaller,
ingeniería inversa permitida para depurar) se difiere a **PR-LIC-02**, tal como ordena la misión. En
particular: **no** se afirma que incluir el texto de la LGPL cumpla la LGPL.

Detalle adicional honesto: el texto base de LGPL en los cinco artefactos es el de la versión 2.1;
la condición "or later" proviene de la declaración del proyecto, no de un aviso por archivo dentro
del artefacto. Queda como pregunta abierta (fijarla con el sdist, no por inferencia).

## 6. Resultado LIC01-C — Hermes y atribuciones preexistentes

- `THIRD_PARTY_NOTICES.md` contiene **una única** sección (Hermes Agent). Se preserva íntegra.
- Verificación adversarial: `sky_claw/app/agent/hermes_parser.py` es un **shim de 8 líneas** que
  reexporta `sky_claw.app.agent.xml_tool_call_parser` (parser propio basado en una regex
  `<tool_call>…</tool_call>` y `json.loads`). No se halló código de Hermes.
- La licencia MIT de Hermes se recuperó del repo (`main`, commit `3ef8bf4f68c9`) para dejar la
  atribución **verificable**, aunque no haya obligación mientras no se redistribuya código.
- **Lo que debe cambiar en el borrador del NOTICE:** la frase actual "no separate Hermes license file
  is included at this time" deja el aviso incompleto si algún día se incorpora código. En el borrador
  (documento 08) se conserva la atribución y se explicita el criterio.

## 7. Lo que este documento NO afirma

1. No afirma que todo lo listado esté dentro de `SkyClawApp.exe`: la reconciliación efectiva
   EXE↔componentes requiere el Analysis TOC (EVIDENCE_MISSING).
2. No afirma la versión exacta de CPython/OpenSSL/SQLite/libffi del ejecutable.
3. No afirma compatibilidad entre licencias ni concluye nada sobre la migración a GPL-3.0.
4. No declara cumplimiento: declara **obligaciones registradas** y **huecos identificados**.

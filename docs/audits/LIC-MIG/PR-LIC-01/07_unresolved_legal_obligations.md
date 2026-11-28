# PR-LIC-01 — 07 · Obligaciones no resueltas, con fase asignada

**Regla de esta lista:** toda obligación pendiente queda (a) identificada, (b) asignada a una fase, o
(c) bloqueada por falta de evidencia. Nada queda "en el aire". Ninguna se marca como resuelta por
aparecer en un documento.

`BLOQUEA RELEASE` = ¿impide publicar un ejecutable con avisos correctos? (no es un juicio de
legalidad, es un juicio de completitud del paquete de avisos).

---

## 1. Bloqueantes de evidencia (afectan a **todo** lo demás)

| ID | Obligación / hueco | Evidencia | Fase | Bloquea release |
|---|---|---|---|---|
| **E-01** | **Reconciliación efectiva EXE↔componentes**: sin `inventory.json`, `sbom.spdx.json` ni Analysis TOC no puede afirmarse archivo por archivo qué viaja en `SkyClawApp.exe`. Todo `REDISTRIBUIDO_EN_EXE` de la matriz se apoya en *alcanzabilidad estática*, no en el inventario real | `EVIDENCE_MISSING` (respaldos `I:\Skyclaw_Backups` no accesibles en este entorno) | **PR-LIC-02** (primera tarea) | **sí** |
| **E-02** | **Versiones exactas de los nativos**: `build_env.json` ausente ⇒ CPython (parche), OpenSSL, SQLite, libffi, expat dentro del EXE quedan `NO VERIFICADO`. Sin versión no hay texto exacto ni avisos exactos | `EVIDENCE_MISSING` | **PR-LIC-02** | **sí** |
| **E-03** | **`webencodings 0.5.1`**: ni el wheel ni el sdist (ambos con SHA-256 verificado) contienen licencia | `EVIDENCE_MISSING` | **PR-LIC-02** | **sí** |
| **E-04** | **`sv-ttk`**: declarada en `pyproject.toml`, importada por ningún módulo. ¿Se congela? | `INFERRED` | **PR-LIC-02** (con Analysis TOC) | no (pero decide si entra al aviso) |

## 2. Obligaciones de copyleft débil (LGPL / MPL) — **no** se resuelven aquí

| ID | Obligación | Componentes | Fase | Bloquea release |
|---|---|---|---|---|
| **H-01** | **Código fuente correspondiente de la LGPL**: disponibilidad de las fuentes de los 5 paquetes LGPL-2.1-or-later en la versión distribuida (y de sus modificaciones, si las hubiera) | `py7zr 1.1.3`, `pybcj 1.0.7`, `pyppmd 1.3.1`, `inflate64 1.0.4`, `multivolumefile 0.2.3` | **PR-LIC-02** | **sí** |
| **H-02** | **Relinking / reemplazo** en un onefile de PyInstaller: cómo permite el destinatario reemplazar la biblioteca LGPL por una versión modificada propia (hoy: no hay mecanismo) | ídem | **PR-LIC-02** | **sí** |
| **H-03** | **Ingeniería inversa permitida para depurar modificaciones** (LGPL §?): declaración explícita en el aviso | ídem | **PR-LIC-02** | no |
| **H-04** | **MPL-2.0 por archivo**: si hay archivos cubiertos modificados, disponibilidad de su fuente. Casos: `bidict` (Limpio), `certifi` (ca-bundle declarado "modified version"), `grpcio` (parte MPL sin mapear), `orjson` (MPL declarada **sin texto**) | ver §2.3 | **PR-LIC-02** | **sí** para `grpcio`/`orjson`/`certifi` |
| **H-05** | **Elección Apache-2.0 vs MPL-2.0 en DOMPurify**: hoy no está declarada; si se elige MPL nace obligación de fuente | `nicegui/static/dompurify.mjs` | **PR-LIC-02** | no |

**Advertencia explícita:** incluir el texto de la LGPL/MPL en `THIRD_PARTY_NOTICES.md` **no** cumple
estas obligaciones. Ese es exactamente el error que la misión prohíbe presentar como cumplimiento.

### 2.1 Qué parte de `grpcio` es MPL
El `LICENSE` agregado contiene el texto completo de MPL-2.0 (cobertura 0,997), pero el wheel no dice
qué archivos cubre. Resolverlo requiere el sdist y su mapa de licencias → PR-LIC-02.

### 2.2 `orjson`: contradicción sin resolver
`License-Expression` = `MPL-2.0 AND (Apache-2.0 OR MIT)`. El wheel contiene `LICENSE-APACHE` y
`LICENSE-MIT`; **no** contiene texto MPL. O el metadato está sobredeclarado, o falta evidencia. No se
resuelve por conveniencia: `EVIDENCE_CONFLICT`.

### 2.3 `certifi`: dos obligaciones distintas
1. **Texto**: el archivo de 989 B nombra MPL-2.0 y no lo reproduce → hay que incorporar el texto
   íntegro (fácil, PR-LIC-04).
2. **Fuente**: si el `ca-bundle.crt` es "Covered Software" modificado, MPL-2.0 §3.1 exige poner a
   disposición la forma modificada (que es, literalmente, el archivo que ya distribuimos) → hay que
   decidir y documentar cómo se satisface (PR-LIC-02).

## 3. Componentes con licencia derivada de GPL

| ID | Obligación | Estado | Fase | Bloquea release |
|---|---|---|---|---|
| **G-01** | **Auto Parallax**: `sky_claw/local/assets/auto_parallax_regla.py` es una transcripción declarada de `src/XSEPlugin.cpp` (upstream **GPL-3.0**, verificado: `COPYING` del upstream + SHA-256 del archivo citado `c70f6e21…88a3`, **reproducido exacto hoy**). Está importado por `sky_claw/local/native_parallax/elegibilidad.py` ⇒ **se distribuye dentro del EXE** de un producto hoy MIT | `LEGAL_REVIEW_REQUIRED` + `REMEDIATION_REQUIRED` | **misión separada de Auto Parallax (PR-LIC-06)** | **sí, mientras el producto sea MIT** |
| **G-02** | Decidir si la transcripción es obra derivada (jurídico, no técnico). No se reescribe, no se relicencia, no se intenta remediación clean-room en esta fase | abierto | PR-LIC-06 | **sí** |

## 4. Recursos gráficos (decisión del propietario)

| ID | Obligación | Estado | Fase | Bloquea release |
|---|---|---|---|---|
| **D-01** | **`parchment.png` / `stone_bg.png`**: manifiesto C2PA declara generación por IA de **Google** (contradice la expectativa de Grok); firma **no validada** (`signingCredential.untrusted`); términos de uso del generador no acreditados (fecha/versión) | `EVIDENCE_CONFLICT` | **PR-LIC-05** | **sí** (permiso de redistribución no acreditado) |
| **D-02** | **`alduin_menace_bg.jpg`**: sólo declaración del propietario; sin EXIF/XMP/C2PA; falta evidencia del generador y análisis de derechos sobre elementos de terceros representados | `OWNER_ATTESTED` | **PR-LIC-05** | decisión del propietario |
| **D-03** | Extensión `.png` sobre datos JPEG en dos assets: riesgo operativo para clasificadores por extensión | hecho verificado | PR-LIC-05 | no |
| **D-04** | Historial de incorporación (commit/autor) irrecuperable en este entorno (clon shallow) | `EVIDENCE_MISSING` | PR-LIC-02 (con respaldos) | no |

## 5. Fuentes

| ID | Obligación | Estado | Fase | Bloquea release |
|---|---|---|---|---|
| **F-01** | Incorporar los **4 textos OFL-1.1** (uno por familia) y el copyright exacto de cada archivo, incluido el RFN de Cinzel | `SUPPORTED_BY_METADATA` | **PR-LIC-04** | no (es entrega de texto) |
| **F-02** | **EB Garamond**: el archivo distribuido es **1.001**; el upstream actual sirve **1.002** para Regular. No se certifica un archivo histórico con la versión actual: fijar la release de origen o actualizar el asset (requiere autorización) | `EVIDENCE_CONFLICT` | **PR-LIC-04 / PR-LIC-05** | no |
| **F-03** | **Material Icons / Symbols / Roboto** (Apache-2.0, dentro de NiceGUI): texto + avisos **no** incluidos en el wheel | `SUPPORTED_BY_METADATA` | **PR-LIC-04** | **sí** (obligación Apache) |

## 6. JavaScript vendorizado (NiceGUI)

| ID | Obligación | Estado | Fase | Bloquea release |
|---|---|---|---|---|
| **J-01** | Textos íntegros de DOMPurify, Vue, Quasar, socket.io-client, Immutable.js (banners MIT/Apache en el archivo, texto ausente en el wheel) | `SUPPORTED_BY_METADATA` | **PR-LIC-04** | **sí** |
| **J-02** | `sass.dart.js` (3,1 MB) sin banner: identificar Dart Sass + runtime Dart y sus avisos | `EVIDENCE_MISSING` | **PR-LIC-02** | **sí** |
| **J-03** | Versiones exactas de esos 7 recursos (no declaradas en los archivos): obtenerlas del `package.json` de NiceGUI 3.12.0 | pendiente | **PR-LIC-02** | no |

## 7. Textos y avisos faltantes en paquetes Python

| ID | Obligación | Componente | Fase | Bloquea release |
|---|---|---|---|---|
| **P-01** | Texto BSD-2 de `decorator 4.0.10` que el `NOTICE` declara empaquetado | `prometheus-client 0.25.0` | **PR-LIC-04** | **sí** (aviso obligatorio) |
| **P-02** | Confirmar si el código de `mkdocstrings-python` (ISC) citado en el `NOTICE` viaja en el paquete | `python-json-logger 3.3.0` | PR-LIC-02 | no |
| **P-03** | Concluir licencia única o régimen mixto documentado | `docutils 0.22.4` | **PR-LIC-02** | **sí** |
| **P-04** | Reproducir **completos** los LICENSE agregados, sin recortar | `numpy`, `pillow`, `grpcio` | **PR-LIC-04** | **sí** |
| **P-05** | Declarar la elección de licencia en los duales | `cryptography` (Apache-2.0 vs BSD-3), `orjson` (Apache-2.0 vs MIT), `pycryptodomex` (Unlicense vs BSD-2) | **PR-LIC-04** | no |
| **P-06** | No exigir atribución a `cffi` (texto **MIT-0**, metadato dice MIT) ni a `h11`/`rarfile` (0BSD): evita un aviso incorrecto | `cffi`, `h11`, `rarfile` | **PR-LIC-04** | no |
| **P-07** | Declarar licencia del propio paquete (`license = "MIT"`, `license-files`) — hoy ausente en `pyproject.toml` | proyecto | **PR-LIC-04** | no |

## 8. Frontera MO2 (bloqueo que se preserva, no se "resuelve")

| ID | Obligación | Estado | Fase | Bloquea release |
|---|---|---|---|---|
| **M-01** | Determinar si `skyclaw_bridge` (que importa `mobase`/PyQt6 y corre **dentro** del proceso de MO2, GPL-3.0) es obra separada o queda alcanzada | `LEGAL_REVIEW_REQUIRED` | **PR-LIC-02** | **sí** |
| **M-02** | `ModOrganizer2/modorganizer-plugin_python` **no declara licencia** (verificado por API): no existe una "licencia de mobase" citable | hecho verificado | PR-LIC-02 | no |
| **M-03** | Mantener `mo2_integration_boundary_pending_legal` vigente y **no** convertirlo en aprobación (el reconciliador ya lo hace) | vigente | — | — |

## 9. Lo que **no** se resuelve en ninguna fase de PR-LIC-01

- La migración MIT → GPL-3.0 (`GPL_MIGRATION_HOLD` **ACTIVO**): este informe no toca `LICENSE`, no
  declara compatibilidad, no opina sobre si la migración es viable con Auto Parallax GPL dentro.
- La publicación (`RELEASE_PUBLICATION_HOLD` **ACTIVO**): B15 y `release.yml` siguen suspendidos.
- El mecanismo de cumplimiento LGPL (PR-LIC-02), la remediación de Auto Parallax (misión aparte), la
  decisión sobre los gráficos (PR-LIC-05).

## 10. Resumen cuantitativo

| Categoría | Cantidad |
|---|---|
| Bloqueantes de evidencia | 4 |
| Obligaciones copyleft débil (LGPL/MPL) | 5 |
| Componentes derivados de GPL | 1 (con 2 sub-obligaciones) |
| Recursos gráficos con decisión pendiente | 3 |
| Fuentes con obligación de texto | 4 familias propias + 3 familias de NiceGUI |
| JS vendorizado con obligación de texto | 5 + 1 sin identificar |
| Textos/avisos faltantes en Python | 7 |
| Frontera MO2 | 3 |
| **Filas de la matriz en `LEGAL_REVIEW_REQUIRED`** | **15** |
| **Filas con `source_obligation = SI`** | **9** |

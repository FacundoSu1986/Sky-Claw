# PR-LIC-01 — 02b · Lectura humana de la matriz de componentes efectivos

Compañero de `02_effective_component_license_matrix.csv` (124 filas × 23 columnas). Este documento
explica **cómo leer** la matriz, resume los agregados y señala lo que un lector apurado
malinterpretaría.

---

## 1. Reglas de lectura (imprescindibles)

1. **`license_declared` ≠ `license_concluded`.** La primera es lo que dice el metadato (PyPI,
   `package.json`, un banner). La segunda es lo que se concluye del **texto de licencia del
   artefacto exacto**, comparado contra la base canónica de SPDX. Cuando sólo hay metadato, la
   matriz lo dice en `evidence_status`.
2. **`evidence_status` ≠ `compliance_status`.** Una licencia perfectamente identificada puede
   convivir con una obligación incumplida. Ejemplo real de esta matriz: certifi tiene
   `license_concluded = MPL-2.0` (identificación sólida) y
   `compliance_status = OBLIGACION_REGISTRADA_TEXTO_FALTANTE` (el wheel **no trae el texto**).
3. **`redistribution_scope` no se deduce del lockfile.** El lock fija versiones; PyInstaller
   congela lo alcanzable desde `sky_claw/__main__.py` más los `hiddenimports` del spec. Por eso hay
   una fila con `NO_ALCANZABLE_POR_IMPORT` (`sv-ttk`) que **está declarada como dependencia del
   proyecto** y probablemente no se congela.
4. **No existe valor `COMPLIANT`.** Deliberadamente. Los estados de cumplimiento posibles son:
   `OBLIGACION_REGISTRADA` (y sus variantes con sufijo), `LEGAL_REVIEW_REQUIRED`,
   `REMEDIATION_REQUIRED`, `REQUIERE_VERIFICACION_CON_ANALYSIS_TOC`, `SIN_OBLIGACION*`,
   `ATRIBUCION_PRESERVADA_SIN_OBLIGACION`.

## 2. Agregados

| Métrica | Valor |
|---|---|
| Filas totales | **124** |
| Distribuciones Python redistribuidas (LIC01-A) | **94** (base 92 · native-parallax 94 · unión 94) |
| Distribuciones con texto de licencia **del artefacto exacto** | **93 / 94** (falta `webencodings`) |
| Alcanzables desde `__main__`/hiddenimports (cierre transitivo) | **93 / 94** |
| Componentes nativos/vendorizados propios (LIC01-B) | **17** |
| Gráficos (LIC01-D) | **3** |
| Fuentes (LIC01-E) | **5** filas (4 familias propias + 1 fila agregada de iconos/Roboto de NiceGUI) |
| Fronteras MO2 (LIC01-F) | **3** |
| Auto Parallax (LIC01-G) | **1** |
| Con obligación de código fuente (`source_obligation = SI`) | **9** (5 LGPL · 2 MPL · 1 MPL agregado · 1 GPL derivado) |

Distribución por licencia concluida (filas):

| Licencia | Filas |
|---|---|
| MIT | 41 |
| Apache-2.0 | 21 |
| BSD-3-Clause | 19 |
| LGPL-2.1-or-later | 5 |
| PSF-2.0 | 4 |
| OFL-1.1 | 4 |
| MPL-2.0 | 2 |
| MIT-0 | 2 |
| BSD-2-Clause | 2 |
| 0BSD / CC0-1.0 / Unlicense / Zlib / HPND | 1 cada una (agregadas de numpy, pillow, pycryptodomex, rarfile) |
| Compuestas / elección dual | 9 filas (numpy, pillow, grpcio, aiohttp, cryptography, orjson, pycryptodomex, backports-zstd, DOMPurify) |
| **Vacías (`license_concluded` = "")** | **10** filas — ver §4 |

Por estado de evidencia:

| Estado | Filas |
|---|---|
| `VERIFIED_EXACT_ARTIFACT` | 88 |
| `SUPPORTED_BY_METADATA` | 12 |
| `EVIDENCE_CONFLICT` | 6 |
| `EVIDENCE_MISSING` | 5 |
| `INFERRED` | 4 |
| `VERIFIED_PRIMARY_SOURCE` | 4 |
| `LEGAL_REVIEW_REQUIRED` | 2 |
| `OWNER_ATTESTED` | 1 |
| `EVIDENCE_EXACT_ARTIFACT_TEXTO_INCOMPLETO` | 1 |
| `NOT_APPLICABLE` | 1 |

## 3. Las 15 filas con `compliance_status = LEGAL_REVIEW_REQUIRED`

Ninguna de ellas es un "teórico": son las que hoy impiden publicar un ejecutable con avisos
completos.

| Componente | Motivo en una línea |
|---|---|
| `docutils` 0.22.4 | COPYING.rst describe régimen mixto (dominio público / BSD-2 / GPL-2 por partes): no hay licencia única concluible |
| `webencodings` 0.5.1 | **ni el wheel ni el sdist** (ambos con hash verificado) contienen archivo de licencia |
| `grpcio` 1.80.0 | LICENSE agregado con texto **MPL-2.0** completo: hay que saber qué archivos son MPL |
| `orjson` 3.11.9 | `License-Expression` declara `MPL-2.0 AND (Apache-2.0 OR MIT)` y el wheel **no** trae texto MPL |
| DOMPurify 3.4.0 (NiceGUI) | dual Apache-2.0 / MPL-2.0; la elección debe ser explícita y hoy no lo es |
| Vue / Quasar / socket.io-client / Immutable.js (NiceGUI) | licencia legible en banner, **pero el texto íntegro no viaja en el wheel** y la versión exacta no es identificable |
| `sass.dart.js` (NiceGUI) | 3,1 MB de JS compilado **sin banner**: ni licencia ni copyright localizables |
| Tailwind + UnoCSS (NiceGUI) | se asume MIT por upstream; el artefacto no lo prueba (`INFERRED`) |
| `alduin_menace_bg.jpg` | sólo declaración del propietario; sin EXIF, sin XMP, sin C2PA |
| `parchment.png` / `stone_bg.png` | manifiesto C2PA que dice **Google Generative AI**; firma no validada; términos no acreditados |
| `skyclaw_bridge` | obra propia ejecutándose **dentro del proceso** de MO2 (GPL-3.0): la independencia no se presume |

## 4. Las 10 filas sin `license_concluded` (y por qué es correcto que estén vacías)

`docutils`, `webencodings`, las tres imágenes (LIC01-D), `sass.dart.js`, la fila agregada de
terceros embebidos en CPython, `PyQt6` (externo), las dependencias Node (no redistribuidas) y el
toolchain de build (no redistribuido).

En ningún caso se completó el campo "por conveniencia". Un campo vacío con
`evidence_status`/`unresolved_questions` poblados es más honesto que una conclusión inventada.

## 5. Filas que un lector apurado malinterpretaría

| Fila | Interpretación errónea | Interpretación correcta |
|---|---|---|
| `sv-ttk` | "está en el lock ⇒ se distribuye" | declarada en `pyproject.toml`, **ningún módulo la importa**; probablemente no se congela. Confirmar con Analysis TOC |
| `pyinstaller-bootloader` | "GPL en el ejecutable ⇒ el proyecto debe ser GPL" | la **Bootloader Exception** del tag exacto 6.22.3 autoriza el embedding sin imponer el GPL al programa combinado |
| `numpy` | "numpy es BSD-3" | la licencia distribuida es un **archivo agregado** con 7 regímenes distintos que debe reproducirse completo |
| `certifi` | "MPL-2.0 ⇒ obligación de fuente" | primero falta el **texto**: el archivo de 989 B sólo nombra MPL-2.0 |
| `cffi` | "MIT" | el texto del artefacto es **MIT-0** (sin cláusula de atribución) aunque el metadato diga MIT |
| `aiohttp` | "Apache-2.0" | Apache-2.0 **y** MIT: el wheel vendoriza `llhttp` con su propio LICENSE |
| `parchment.png` / `stone_bg.png` | "PNG" | son **JPEG** con extensión `.png`, con manifiesto C2PA de Google |
| `auto_parallax_regla.py` | "módulo propio más" | transcripción de `XSEPlugin.cpp` GPL-3.0, **importada** por `native_parallax/elegibilidad.py` |
| `Hermes Agent` | "hay que agregar su licencia" | no se redistribuye código de Hermes; la atribución existente se preserva por continuidad |
| `telegram-node-deps` | "el gateway distribuye grammy/ws" | `node_modules` no está versionado; el gateway no viaja en el EXE |
| `build-only-toolchain` | "PyInstaller/hatchling se distribuyen" | excluidos explícitamente por el spec (B08); **sólo** bootloader y rthooks viajan |

## 6. Columnas con valor diagnóstico más alto

- `artifact_sha256`: hash del artefacto **que contenía el texto citado** (no el del primer artefacto
  descargado). Permite re-verificar sin confiar en este informe.
- `evidence_sha256`: hash del/los archivo(s) de licencia extraídos. Con `license_text_source` se
  reconstruye la cadena `lock → artefacto → texto`.
- `unresolved_questions`: 100% poblado donde hay incertidumbre; vacío sólo donde no la hay.
- `remediation_phase`: `PR-LIC-02` (obligaciones de fuente/relinking y versiones), `PR-LIC-04`
  (empaquetado de avisos), `PR-LIC-05` (decisión del propietario sobre gráficos), `PR-LIC-06`
  (Auto Parallax).

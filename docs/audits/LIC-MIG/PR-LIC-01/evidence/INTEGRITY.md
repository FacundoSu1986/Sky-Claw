# Integridad y manifiesto del paquete PR-LIC-01

**Generado:** 2026-11-28 (UTC)
**Árbol:** `docs/audits/LIC-MIG/PR-LIC-01/`
**Correspondencia con la ruta ordenada:** este árbol es el contenido de
`I:\Skyclaw_Backups\LIC-MIG-AUDIT\PR-LIC-01\` (esa ruta no es escribible en el entorno de auditoría;
ver `01_preflight.md` §3 y §8).

---

## 1. Entregables ordenados (1–11)

| # | Archivo | Estado |
|---|---|---|
| 1 | `01_preflight.md` | presente |
| 2 | `02_effective_component_license_matrix.csv` | presente (124 filas × 23 columnas) |
| 2b | `02b_matrix_reading_guide.md` | **adicional** — lectura humana de la matriz exigida por la misión (§9) |
| 3 | `03_python_native_vendored_license_audit.md` | presente |
| 4 | `04_graphics_and_fonts_provenance.md` | presente |
| 5 | `05_mo2_and_external_tools_boundaries.md` | presente |
| 6 | `06_attribution_and_notice_requirements.md` | presente |
| 7 | `07_unresolved_legal_obligations.md` | presente |
| 8 | `08_third_party_notices_draft.md` | presente (generado) |
| 9 | `09_implementation_brief.md` | presente |
| 10 | `10_acceptance_tests.md` | presente |
| 11 | `SHA256SUMS.txt` | presente |

## 2. Evidencia primaria y material de soporte

| Directorio | Contenido | Volumen |
|---|---|---|
| `evidence/pypi/` | `metadata.json` + textos de licencia de cada artefacto exacto | 94 paquetes, 5,7 MB |
| `evidence/pypi/analisis_licencias.json`, `resumen_licencias.csv` | clasificación y coberturas | 2 archivos |
| `evidence/pypi/resumen.json` | resultado del colector (hashes, artefactos, errores) | 1 archivo |
| `evidence/github/` | textos primarios (CPython, PyInstaller, OpenSSL, libffi, SQLite, Tcl, MO2, usvfs, plugin_python, Hermes, NiceGUI, Auto Parallax, CommonLibSSE-NG, 4 × OFL) + `registro_github.json` | 23 archivos de 15 repos |
| `evidence/graphics/` | manifiestos C2PA completos en JSON | 2 manifiestos |
| `evidence/fonts/` | `name` tables de 16 WOFF2 de NiceGUI, 3 TTF upstream, copias de los WOFF2 de NiceGUI | 1 + 2 JSON |
| `evidence/nicegui_static_manifest.json` | manifiesto de los 115 estáticos del wheel de NiceGUI con SHA-256 y banners | 1 archivo |
| `evidence/redistribucion.json` | cierre de alcanzabilidad (imports + hiddenimports + requires_dist) | 1 archivo |
| `evidence/componentes_no_pypi.json` | filas curadas de componentes no-PyPI con su justificación | 1 archivo |
| `evidence/matriz_resumen.json` | agregados de la matriz | 1 archivo |
| `evidence/registry/` | `urls_y_fechas.jsonl` (PyPI) + `fuentes_consolidadas.md` (todas las fuentes) | 2 archivos |
| `evidence/scripts/` | 6 scripts de investigación (reproducibles, sin red en los pasos de análisis) | 6 archivos |
| `license_texts/` | 16 textos canónicos + `INDICE.md` | 17 archivos |

## 3. Scripts y qué produce cada uno

| Script | Produce | Requiere red |
|---|---|---|
| `colectar_metadatos_pypi.py` | `evidence/pypi/**`, `registry/urls_y_fechas.jsonl` | sí (pypi.org, files.pythonhosted.org) |
| `clasificar_licencias.py` | `evidence/pypi/analisis_licencias.json`, `resumen_licencias.csv` | no (requiere una copia de `license-list-data/text`) |
| `colectar_textos_github.py` | `evidence/github/**`, `registro_github.json` | sí (api.github.com) |
| `analizar_redistribucion.py` | `evidence/redistribucion.json` | no |
| `construir_matriz.py` | `02_…matrix.csv`, `evidence/matriz_resumen.json` | no |
| `consolidar_paquetes.py` | `license_texts/**`, `evidence/registry/fuentes_consolidadas.md` | no |
| `generar_borrador_notices.py` | `08_third_party_notices_draft.md` | no |

## 4. Fuentes externas y su sellado

| Fuente | Ref / versión | SHA-256 registrado |
|---|---|---|
| `pypi.org` JSON API + `files.pythonhosted.org` | 94 artefactos, versión exacta del lock | sí, por artefacto, **y verificado contra el hash del lock** |
| `api.github.com` | 23 archivos con ref fija y commit resuelto | sí, por archivo (`registro_github.json`) |
| `codeload.github.com/spdx/license-list-data` | rama `main`, 2026-11-28 | tarball `084868b7c2be0019f3a7fa1460a1e71a93f1236ee6545e657843f00bd1c13b4d` |
| Lector C2PA (`c2pa-python`) | — | no aplica (herramienta) |

## 5. Advertencias de integridad

1. **Los hashes de PyPI se verificaron dos veces**: contra el JSON de PyPI y contra el `--hash` del
   lockfile del repo. Un artefacto que no coincidiera con el lock se habría rechazado (0 rechazos).
2. **Nada se transcribió de memoria**: si una fuente no respondía, la fila quedó `EVIDENCE_MISSING`.
3. **Los textos canónicos de SPDX no sustituyen** a los del artefacto: la regla de precedencia está
   en `license_texts/INDICE.md`.
4. **La cadena C2PA no se validó**: `signingCredential.untrusted` + `ocsp.skipped`. Los manifiestos se
   guardan íntegros para que un tercero pueda revalidarlos con anclas de confianza.
5. Este manifiesto **no** incluye hashes de archivos del producto (código, assets): sólo de la
   evidencia y de los entregables, que es lo que la misión pide sellar.

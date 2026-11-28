# PR-LIC-01 — 10 · Criterios y pruebas de aceptación de la fase

**Qué valida este documento:** que la *fase de investigación* cumplió lo que prometió. **No** valida
el producto, **no** valida los avisos definitivos y **no** certifica nada.

Cada criterio de la misión (§13) se traduce en: (a) un hecho observable, (b) el artefacto donde se
verifica, (c) el comando reproducible.

---

## 1. Tabla de aceptación (criterios de la misión → verificación)

| # | Criterio de la misión | Hecho observable | Artefacto | Verificación reproducible | Estado |
|---|---|---|---|---|---|
| A1 | Componentes efectivos identificados y reconciliados con el inventario | 124 filas; 94 distribuciones Python; 93/94 alcanzables; la reconciliación **efectiva** contra el EXE se declara imposible (EVIDENCE_MISSING) en lugar de fingirse | `02_…matrix.csv`, `evidence/redistribucion.json` | `python3 evidence/scripts/analizar_redistribucion.py --repo /ruta --pypi-dir evidence/pypi --salida evidence/redistribucion.json` | **PARCIAL — aceptado con limitación declarada** |
| A2 | Cobertura verificable de las atribuciones requeridas | 93/94 textos extraídos del artefacto exacto (hash del artefacto == hash del lock); 118 componentes redistribuidos con aviso en el borrador | `evidence/pypi/*/`, `08_…draft.md` | `grep -c "^|" 08_third_party_notices_draft.md`; `evidence/registry/fuentes_consolidadas.md` | **SÍ** |
| A3 | Los textos de licencia tienen procedencia documentada | cada texto con URL + fecha + SHA-256; los estándar desde SPDX con hash del tarball; los de componente desde GitHub con ref fija y commit | `license_texts/INDICE.md`, `evidence/registry/` | `sha256sum -c SHA256SUMS.txt` | **SÍ** |
| A4 | Las incertidumbres no se ocultan | 15 filas `LEGAL_REVIEW_REQUIRED`, 5 `EVIDENCE_MISSING`, 6 `EVIDENCE_CONFLICT`, 1 `OWNER_ATTESTED`; 10 filas con `license_concluded` vacío y su pregunta abierta | matriz (columnas `evidence_status`, `unresolved_questions`) | `python3 -c "import csv;…"` (ver §3, comando C4) | **SÍ** |
| A5 | Recursos gráficos y tipográficos inventariados | 3 gráficos con SHA-256, formato real, dimensiones y C2PA leído; 12 WOFF2 propios + 16 de NiceGUI con `name` table verificado y comparación upstream | `04_…md`, `evidence/graphics/`, `evidence/fonts/` | `python3 evidence/scripts/…` (ver §3, C5) | **SÍ** |
| A6 | Componentes pendientes de LGPL identificados | 5 LGPL-2.1-or-later + 3 MPL-2.0 (+ DOMPurify como alternativa), con `source_obligation = SI` y fase PR-LIC-02 | `03_…md` §5, `07_…md` §2 | `grep "LGPL" 02_…csv` | **SÍ** (identificados, **no** resueltos) |
| A7 | Fronteras MO2 y Auto Parallax conservan sus bloqueos | `mo2_integration_boundary_pending_legal` vigente; Auto Parallax en `LEGAL_REVIEW_REQUIRED` + `REMEDIATION_REQUIRED` | `05_…md` §4, `07_…md` §3/§8 | `grep -n "mo2_integration_boundary_pending_legal" scripts/reconciliar_sbom.py` | **SÍ** |
| A8 | Borrador coherente de `THIRD_PARTY_NOTICES.md` | 314 líneas generadas de la matriz, con secciones de proyecto, textos, componentes por licencia, avisos especiales, copyleft débil, gráficos, atribuciones preservadas y pendientes | `08_…draft.md` | `python3 evidence/scripts/generar_borrador_notices.py --matriz 02_…csv --salida /tmp/x.md && diff /tmp/x.md 08_…draft.md` | **SÍ** |
| A9 | Write-set mínimo para implementación | 12 archivos propuestos (3 modificados), justificados uno por uno, con rollback y estimación | `09_…md` §2 | lectura | **SÍ** |
| A10 | Evidencia preservada y sellada con hashes | `SHA256SUMS.txt` sobre todos los entregables + evidencia; manifiesto de integridad con conteos | `SHA256SUMS.txt`, `evidence/INTEGRITY.md` | `sha256sum -c SHA256SUMS.txt` | **SÍ** |
| A11 | Holds de migración y distribución vigentes | `LICENSE` MIT intacto; B15 `if: ${{ false }}`; `release.yml` pausado; cero archivos productivos modificados | `01_preflight.md` §9 | `git status --porcelain` (sólo `docs/audits/LIC-MIG/PR-LIC-01/**` nuevo) | **SÍ** |

**Limitación declarada de A1 (la única parcial):** sin `inventory.json`/Analysis TOC/`build_env.json`
no se puede afirmar la composición archivo por archivo del EXE ni las versiones exactas de los
nativos. Se sustituyó por alcanzabilidad estática **etiquetada como tal** (`SUPPORTED_BY_METADATA`,
`INFERRED`), nunca presentada como inventario. Prioridad #1 de PR-LIC-02.

## 2. Criterios que **no** se cumplen y no deben cumplirse en esta fase

| Criterio habitual | Por qué no se exige aquí |
|---|---|
| "Todos los componentes tienen licencia concluida" | sería falso: 10 filas no la tienen y 15 requieren revisión jurídica. Inventarla sería el peor resultado posible |
| "El aviso está listo para publicar" | hay obligaciones de fuente abiertas (LGPL/MPL) y textos faltantes: publicar sería declarar cumplimiento inexistente |
| "Compatibilidad GPL verificada" | prohibido expresamente; además Auto Parallax (GPL) está dentro de un producto MIT |
| "B15 / release listos" | `RELEASE_PUBLICATION_HOLD` ACTIVO |

## 3. Comandos de verificación de esta entrega

```bash
cd docs/audits/LIC-MIG/PR-LIC-01

# C1 — integridad de todo el paquete
sha256sum -c SHA256SUMS.txt

# C2 — la matriz tiene las 23 columnas y 124 filas
python3 - <<'PY'
import csv
rows=list(csv.DictReader(open("02_effective_component_license_matrix.csv",encoding="utf-8")))
assert len(rows[0])==23, len(rows[0])
print(len(rows), "filas;", len(rows[0]), "columnas")
PY

# C3 — no hay estados de cumplimiento "genéricos"
grep -c "COMPLIANT" 02_effective_component_license_matrix.csv || echo "0 ocurrencias de COMPLIANT (correcto)"

# C4 — incertidumbre visible
python3 - <<'PY'
import csv, collections
rows=list(csv.DictReader(open("02_effective_component_license_matrix.csv",encoding="utf-8")))
print(collections.Counter(r["evidence_status"] for r in rows))
print("sin licencia concluida:", sum(1 for r in rows if not r["license_concluded"]))
print("con pregunta abierta:", sum(1 for r in rows if r["unresolved_questions"].strip()))
PY

# C5 — fuentes y gráficos: hashes de los WOFF2 propios
python3 - <<'PY'
import hashlib, pathlib
for p in sorted(pathlib.Path("../../../../sky_claw/app/gui/assets/fonts").glob("*.woff2")):
    print(hashlib.sha256(p.read_bytes()).hexdigest(), p.name)
PY

# C6 — el borrador de avisos se regenera igual (no drift)
python3 evidence/scripts/generar_borrador_notices.py \
  --matriz 02_effective_component_license_matrix.csv --salida /tmp/notices.md >/dev/null
diff -q /tmp/notices.md 08_third_party_notices_draft.md && echo "[OK] sin drift"

# C7 — holds: el repo no fue modificado salvo este árbol de auditoría
cd /home/user/Sky-Claw && git status --porcelain | grep -v "docs/audits/LIC-MIG/PR-LIC-01/" || echo "[OK] sin cambios fuera del árbol de auditoría"
```

## 4. Pruebas negativas de la entrega (deben fallar si alguien falsea el resultado)

| # | Escenario | Detección esperada |
|---|---|---|
| N1 | Editar un CSV para "resolver" una fila sin evidencia | `sha256sum -c SHA256SUMS.txt` falla |
| N2 | Copiar un `license_concluida` de metadatos a mano | inconsistente con `evidence/pypi/*/analisis_licencias.json` (cobertura declarada) |
| N3 | Reemplazar el texto de un componente por "la versión actual de upstream" | `evidence_sha256` deja de coincidir con el archivo del artefacto sellado |
| N4 | Declarar C2PA "validado" | el registro dice `signingCredential.untrusted`; contradicción detectable |
| N5 | Afirmar la versión de CPython del EXE | ninguna fila la afirma; `EVIDENCE_MISSING` en `build_env.json` |
| N6 | Publicar el borrador como aviso definitivo | el propio borrador lleva el banner DRAFT y los ⚠ |
| N7 | Bajar el contador `mo2_integration_boundary_pending_legal` para "limpiar" el bloqueo | `05_…md` §1 documenta que el número real exige Analysis TOC; el reconciliador lo recalcula |

## 5. Criterios de aceptación para la fase siguiente (PR-LIC-02), heredados de aquí

1. Recuperar `inventory.json`, `sbom.spdx.json`, `reconciliation_report.json`, `build_manifest.json`,
   `build_env.json` y Analysis TOC desde `I:\Skyclaw_Backups\LIC-MIG-AUDIT\` y **verificar sus
   `SHA256SUMS`** antes de usarlos.
2. Reconciliar cada fila de esta matriz con su presencia real en el EXE (3 estados: presente,
   ausente, desconocido) y bajar a `EVIDENCE_MISSING` las que no se puedan resolver.
3. Resolver E-01…E-04 y H-01…H-05 de `07_…md`.
4. No reabrir PR-LIC-03A/03B: se reutilizan sus herramientas, no se modifican.

## 6. Declaración final de esta fase

La fase de investigación se considera **satisfactoria con una limitación declarada** (A1: falta el
inventario efectivo del ejecutable en este entorno). No se ocultó ninguna incertidumbre, no se
inventó ninguna licencia, no se modificó ningún archivo productivo y los holds siguen vigentes.

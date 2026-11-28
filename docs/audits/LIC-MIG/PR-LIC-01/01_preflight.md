# PR-LIC-01 — 01 · Preflight (solo lectura)

**Fecha de ejecución:** 2026-11-28 (UTC)
**Fase:** Fase A de PR-LIC-01 — Atribuciones, licencias de terceros y procedencia.
**Naturaleza:** investigación, reconciliación y diseño. **Cero cambios productivos.**
**Holds vigentes al cierre:** `GPL_MIGRATION_HOLD` ACTIVO · `RELEASE_PUBLICATION_HOLD` ACTIVO (ver §9).

---

## 1. Baseline exacto

| Elemento | Valor verificado | Método |
|---|---|---|
| Commit de `main` bajo análisis | `18bbdbdf2568007c2270bf2ace67d3d986ed1d7d` | `git rev-parse HEAD` |
| Rama de trabajo del sandbox | `arena/3e28f113-sky-claw` (bifurcada de ese commit) | `git branch` |
| Último commit (una sola confirmación en el clon) | `18bbdbd feat(build): build Windows controlado, trazabilidad y suspensión B15 (PR-LIC-03B) (#1033)` | `git log -1` |
| `git status` al iniciar | limpio | `git status` |
| CI del commit | `46140750321` — **success** (Sky-Claw CI/CD, push on main) | `gh run list --branch main` |
| CodeQL del commit | `46140750672` — success | `gh run list` |
| "Main en verde" posterior | `46142046819` — success | `gh run list` |

**Confirmación explícita:** el baseline coincide con el `main` confirmado por la misión. No hubo deriva entre el SHA citado en la misión y el `HEAD` inspeccionado.

## 2. Radar anti-colisión

PRs abiertos al momento del preflight (`gh pr list --state open`):

| PR | Título | Rama | Riesgo para PR-LIC-01 |
|---|---|---|---|
| #1034 | `fix(gui): fail closed al desactivar la clave de busqueda (H08-F1 R2)` | `fix/h08-f1-search-credential-delete-fail-closed` | toca GUI; no toca `THIRD_PARTY_NOTICES.md`, `LICENSE`, `sky_claw.spec` ni assets → **sin colisión de write-set** |
| #1028 | `fix(db): toda apertura standalone de SQLite convierte a WAL…` | `feat/bodyslide-pr2c1-siembra-n95jow` | no toca avisos legales → sin colisión |
| #1027 | `feat(grass-cache): P1 veredicto por evidencia…` | `feat/ngio-p1-veredicto-watchdog` | no toca avisos legales → sin colisión |

Ninguno de los PR abiertos modifica `LICENSE`, `THIRD_PARTY_NOTICES.md`, `sky_claw.spec` ni los recursos de `sky_claw/app/gui/assets/`.

## 3. Limitación material del entorno (EVIDENCE_MISSING declarada, no disimulada)

La misión ordena recuperar evidencias de `I:\Skyclaw_Backups\LIC-MIG-AUDIT\`. **Esa ruta no existe en este entorno de ejecución** (sandbox Linux). Se verificó explícitamente:

- `ls /mnt/` → sin unidad `I:`; `find / -maxdepth 4 -name "Skyclaw_Backups"` → sin resultados.

Consecuencia, registrada como **EVIDENCE_MISSING** y **no reconstruida como si fuera original**:

| Evidencia ordenada | Estado en este entorno | Efecto sobre el dictamen |
|---|---|---|
| `inventory.json` (PR-LIC-03A) | **EVIDENCE_MISSING** | la composición efectiva del EXE no puede citarse archivo por archivo |
| `sbom.spdx.json` | **EVIDENCE_MISSING** | no se reutiliza; se reconstruye evidencia *nueva* desde fuentes primarias (abajo) |
| `reconciliation_report.json` | **EVIDENCE_MISSING** | no se cita ningún veredicto previo |
| `build_manifest.json`, `build_env.json` | **EVIDENCE_MISSING** | **la versión exacta de CPython/OpenSSL/SQLite/libffi dentro del EXE queda sin verificar** |
| Analysis TOC del build controlado | **EVIDENCE_MISSING** | no se puede probar presencia/ausencia por archivo; se usa alcanzabilidad estática como sustituto **declarado como tal** |
| Matrices previas de licencias, evidencias de procedencia de gráficos/fuentes | **EVIDENCE_MISSING** | no se aceptan por autoridad; toda conclusión de este informe se apoya en evidencia recuperada hoy |
| `SHA256SUMS` de respaldos previos | **EVIDENCE_MISSING** | no hay nada que verificar; se sellan los artefactos nuevos |

**No se ejecutó ningún build nuevo.** La misión autoriza obtener un inventario actualizado sólo si hay deriva material; sin `build_env.json` ni inventario preservado no existe un punto de comparación que permita *afirmar* deriva, y el entorno no es Windows (el build controlado es Windows-only). Se deja constancia: **la reconciliación efectiva EXE↔componentes queda pendiente y es la primera tarea de PR-LIC-02/04** (ver `07_unresolved_legal_obligations.md` §1).

## 4. Qué se hizo en lugar de citar la autoridad de auditorías previas

Se construyó evidencia primaria nueva, reproducible y sellada:

| Fuente | Volumen | Naturaleza |
|---|---|---|
| Lockfiles del repo (`requirements.lock`, `requirements-native-parallax.lock`, `requirements-build.lock`) | 92 / 94 / 16 distribuciones | fijan versión exacta y **hashes de artefacto** |
| PyPI JSON API por versión exacta + descarga del artefacto con **SHA-256 verificado contra el lock** | 94 distribuciones, 93 con texto de licencia extraído del artefacto | **VERIFIED_EXACT_ARTIFACT** |
| GitHub Contents API con **ref fija** (tags/ramas resueltas a commit) | 23 archivos de 15 repositorios | **VERIFIED_PRIMARY_SOURCE** |
| `spdx/license-list-data` (rama `main`) como base canónica de textos | 831 textos | referencia para medir cobertura |
| Lector C2PA oficial sobre los recursos gráficos | 3 assets | veredicto criptográfico real, no supuesto |
| `fontTools` sobre los 12 WOFF2 propios, los 16 de NiceGUI y los TTF upstream | 31 fuentes | metadatos internos verificados |

## 5. Inspección del checkout (sin alterarlo)

| Elemento | Hallazgo |
|---|---|
| `LICENSE` | MIT, *Copyright (c) 2025 FacundoSu1986* — **vigente**, sin cambios |
| `THIRD_PARTY_NOTICES.md` | **sólo** una sección: Hermes Agent (Nous Research, MIT). No hay ninguna entrada para dependencias Python, nativas, gráficos, fuentes, MO2 ni Auto Parallax |
| `README.md` | badge y sección de licencia: MIT |
| `pyproject.toml` | **sin campo `license` ni `license-files` ni classifiers de licencia**: el metadato del paquete no declara licencia (hallazgo, ver `06`) |
| `sky_claw.spec` | onefile, `upx=True`, `datas` con assets/styles/xedit/shim/bridge/policy; `hiddenimports` (69 entradas); `excludes` con toolchain de build (B08); export opt-in del Analysis TOC por `SKYCLAW_EXPORT_ANALYSIS_TOC`; `VS_VERSION_INFO` con `LegalCopyright` MIT |
| `build.bat` | 4 pasos: uv toolchain → `uv sync --extra dev` → pytest → `scripts/build_controlado.py --profile base` |
| `.github/workflows/ci.yml` | B15: `Upload frozen executable` con `if: ${{ false }}` (publicación suspendida). Jobs de inventario/SBOM/build controlado con `--verify-report` activos |
| `.github/workflows/release.yml` | `workflow_dispatch` únicamente y `if: ${{ false }}` en el job → release pausado |
| `scripts/` (PR-LIC-03A/B) | `inventario_ejecutable.py`, `reconciliar_sbom.py`, `build_controlado.py` **no se modificaron** (se leyeron para entender los contratos: clasificación de origen técnico, blockers jurídicos, gate) |

## 6. Contratos técnicos que condicionan esta fase (leídos, no modificados)

- `reconciliar_sbom.py` emite `licenseDeclared` desde metadatos y **nunca** copia a `licenseConcluded` (queda `NOASSERTION`): la conclusión documental es precisamente lo que aporta PR-LIC-01.
- Blockers jurídicos que el reconciliador ya reconoce: `pending_legal_review=`, `mo2_integration_boundary_pending_legal=`.
- `inventario_ejecutable.py` clasifica `IA_DECLARADA_O_C2PA`, `ADAPTACION_UPSTREAM_GPL`, `MO2_PLUGIN_IN_PROCESS`, `GOOGLE_FONTS_OFL` con estado `PENDIENTE_REVISION_JURIDICA`.
- `build_controlado.py`: un blocker desconocido falla cerrado; los jurídicos conocidos se preservan sin otorgar certificación legal (`legal_certification: False`).

## 7. Deriva entre el inventario preservado y `main`

**No evaluable** (no hay inventario preservado en este entorno). Lo que **sí** se verificó: el árbol de recursos que condiciona atribuciones no cambió respecto de lo que la misión describe (los 3 gráficos, 12 WOFF2 propios, 4 archivos del bridge, `auto_parallax_regla.py` con la misma cabecera que cita el SHA del upstream).

## 8. Dónde vive esta evidencia

En este entorno, la ruta `I:\Skyclaw_Backups\LIC-MIG-AUDIT\PR-LIC-01\` no es escribible. Todo el paquete se generó en:

```
docs/audits/LIC-MIG/PR-LIC-01/         (árbol de entrega, 11 archivos + apéndices)
docs/audits/LIC-MIG/PR-LIC-01/evidence/ (evidencia primaria, scripts, registros)
docs/audits/LIC-MIG/PR-LIC-01/license_texts/ (textos de licencia + índice)
```

**Sin commit y sin PR** (prohibido por la misión). La correspondencia con la ruta Windows está en `SHA256SUMS.txt` §«Correspondencia de rutas»: el árbol se puede copiar tal cual a `I:\Skyclaw_Backups\LIC-MIG-AUDIT\PR-LIC-01\`.

## 9. Estado de los holds

| Hold | Estado | Evidencia del estado |
|---|---|---|
| `GPL_MIGRATION_HOLD` | **ACTIVO** | `LICENSE` sigue siendo MIT; `pyproject.toml` no declara licencia; no se modificó ningún archivo del repo |
| `RELEASE_PUBLICATION_HOLD` | **ACTIVO** | B15 con `if: ${{ false }}`; `release.yml` con job `if: ${{ false }}` y sólo `workflow_dispatch`; no se publicó ningún ejecutable |
| Publicación de `SkyClawApp.exe` | **suspendida en todos los eventos** | `ci.yml` (B15) + `release.yml` |

## 10. Declaración de alcance del preflight

Este preflight **no** certifica cumplimiento, **no** afirma compatibilidad GPL, **no** concluye licencias por metadato y **no** reemplaza la revisión jurídica. Su único producto es el conjunto de hechos verificables sobre los que se apoyan los documentos 02–10.

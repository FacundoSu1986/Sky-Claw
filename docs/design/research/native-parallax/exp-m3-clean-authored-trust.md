# EXP-M3 — Clean authored corpus / normal-only trust decision

> Research-only. Sin DDS, BC5 real, Skyrim, MO2, PGPatcher, DynDOLOD, IA ni ParallaxR.
> **Estado: `EXP_M3_DATA_INSUFFICIENT` — Cohorte A = 0 (HARD STOP §3 disparado).
> Diagnóstico Cohorte B (EVALUATION_DIAGNOSTIC_ONLY) incluido; NO es veredicto.**
> *Nota: el spec llegó truncado a mitad de §35 ("NO volver a probar SOFT hast…"); se
> siguió hasta allí y se aplicaron las convenciones de M1/M2 para gates/commit/PR.*

## Repository/base

Verificado contra GitHub (§4, sin SHAs recordados): `MAIN_SHA=3c9b134` · **#617 OPEN
`14b23b0`** · **#618 OPEN `e2a449b`** · **#620 OPEN `e23bf7a`** (base = rama de #618) —
ancestría verificada; rama nueva `research/native-parallax-exp-m3-clean-authored-trust`
desde el head EXACTO de #620. Segundo reset del sandbox entre sesiones (re-clone
shallow @ `30439a1`): working tree verificado byte-a-byte contra el árbol de `e23bf7a`
(33/33 archivos idénticos), estado ajeno preservado en `stash@{0}`, nada reescrito.

## Dataset provenance — intento y HARD STOP

**Todas las fuentes primarias de binarios están bloqueadas o no existen en GitHub:**

| fuente | resultado |
|---|---|
| ambientcg.com + sus CDNs (backblaze, acg-media, releases.pbr.one) | BLOQUEADOS (curl 000) |
| HuggingFace (MatSynth) | BLOQUEADO (TLS EOF) |
| Poly Haven (api/dl) | BLOQUEADO |
| API oficial ambientCG v2 (JSON, accesible vía fetch) | sin hashes de archivo expuestos |
| Kimbatt/cc0-textures (torrents OFICIALES cc0textures.com; piece-hashes = hashes upstream) | verificable PERO payload 4K–12K; los mirrors accesibles son 2K-JPG → **match byte-a-byte imposible** |
| ShareTextures repo oficial | sólo código del sitio |
| mirrors EXP-M2 | MIRROR_ONLY — §2/§8 prohíben usarlos como evidencia principal |

Per **§8**: no sustituir silenciosamente con mirrors. Per **§3**: **HARD STOP** —
el experimento estadístico principal NO corre. El hard-stop está implementado en
código (`check_cohort_a_sufficient` → `EXP_M3_DATA_INSUFFICIENT`) y testeado.

## Cohorte A — CLEAN_BY_PROVENANCE

Vacía por fuerza mayor de red. El contrato de procedencia está implementado y
testeado (`load_cohort_a`: sólo `PRIMARY_VERIFIED`/`OFFICIAL_HASH_VERIFIED`; campos
obligatorios provider/official_source/sha256/licencia). El manifest
`data/exp-m3-clean-authored-manifest.json` registra el rastro de intentos y el hash
del propio manifest (`4ee2ffa2…`). Re-ejecutable sin cambios de código cuando haya
fuente primaria: basta llenar `cohort_a.assets`.

## Cohorte B — EVALUATION_DIAGNOSTIC_ONLY (construcción)

Filtro pre-registrado: `height_std ≥ 2/255` (no-plano) Y `oracle_agreement < {20,30,40}°`
(sensibilidad §12; 30° NO normativo). El height se usó SÓLO para pertenencia a la
cohorte; ninguna feature lo recibe (mismas APIs §13 de M2). Etiqueta obligatoria en
cada fila. Procedencia M2 = MIRROR_ONLY (explícitamente NO evidencia principal).

| filtro | n | familias | catastróficos |
|---|---:|---:|---:|
| 20° | 14 | 7 | 4 |
| 30° | 20 | 10 | 7 |
| 40° | 23 | 11 | 8 |

## RAW baseline (dentro de B@30°)

med rmse 0.0404 · med var_ratio 0.803 · med corr 0.894. El núcleo sigue siendo real
en coherentes: brick/rock/snow/tiles con var 0.81–1.00 (consistente con M2).

## Coherence characterization — el hallazgo incómodo

Los **7 catastróficos de B@30° tienen agreement 7°–27°** — pasan el filtro de
coherencia global angular y AUN así son catastróficos por la regla de estructura
(Bricks066 corr 0.41/seam; Concrete012 corr 0.08; Ground037 corr 0.008; Road001
0.23; Metal062C 0.03; Planks012 0.64/var 0.41; WoodFloor030 0.51/0.26). La
coherencia angular global (mediana con strength global fitada) **no garantiza
identidad estructural** banda a banda: el "mismatch artístico" es más sutil que un
escalar. Esto debilita la hipótesis de contaminación simple.

## Pre-registración (§27, antes de mirar proxies en B)

`CATASTROPHIC_RECONSTRUCTION := |corr| < 0.5 OR variance_ratio < 0.5`
(invariante a escala authored; var con escala afín compartida del RAW del asset).
Regla fija en `CATASTROPHIC_RULE`, congelada por test.

## Proxies vs outcome (B@30°; Spearman [IC95 bootstrap], AUC cat)

| proxy | ρ (rmse) | IC95 | AUC cat |
|---|---:|---|---:|
| quantization_proxy | −0.24 | [−0.42,+0.45] | 0.50 |
| projection_residual_median | −0.23 | [−0.55,+0.18] | 0.20 |
| cluster/low-trust (2 umbrales pre-registrados) | −0.16…−0.22 | todos incluyen 0 | 0.39–0.45 |
| blockiness_ratio | +0.11 | [−0.44,+0.55] | 0.63 |
| curl_mad / curl_p95 | +0.06/+0.10 | incluyen 0 | 0.35 |
| nz_p05 / nz_p01 / nz_min | +0.05/+0.03/+0.03 | incluyen 0 | 0.71/0.73/0.77 |

**Ningún IC excluye el 0.** Consistencia familiar: los rangos de ρ por familia van de
−1.0 a +1.0 (sin dirección estable). Sensibilidad del umbral: @20° y @40° la
conclusión no cambia (mejor |ρ|=0.32/0.20, CIs que incluyen 0; nz_min AUC
0.78/0.66). **§29 (honestidad): n=14–23 con 4–8 catastróficos → potencia baja; esto
es "sin evidencia de predicción", no "evidencia de ausencia".**

## RATES (§31, curva, sin threshold productivo)

Mejor proxy operable (nz_min): para FALSE_SAFE=0% hay que rechazar el **90%** del
corpus (auto=10%, false_review 61%); con auto=40%: FALSE_SAFE 14% y 50% revisado.
(quantization_proxy resultó degenerado ≡1.0 en JPG — el selector de la curva ahora
excluye proxies sin varianza; corregido y anotado.) **Nada operativo.**

## σ_eff independiente y bandas {2,6} (§33/§34/§15)

Con σ_eff = curl_mad o projection_median (independientes de nz por construcción,
garantizado por test): **r_p01 < 2 para 20/20 assets** — como escala de ruido ambos
residuos superan al percentile nz en órdenes de magnitud; todo el corpus caería en
REJECT, absurdo (7/20 lo son). Las bandas {2,6} vuelven a quedar **NOT_EVALUABLE**,
ahora por exceso y no por circularidad. Conclusión §14: ni la interpretación A
(σ_eff estable) ni la B (risk_score simple) reciben soporte en B; no se conserva la
forma nz/σ por razones arquitectónicas.

## TWO_CHANNEL_Q8_PROXY (§18)

Contadores separados mantenidos (radicando inválido / z=0 / nz<0 real); sin análisis
nuevo en B (sin.encoding aplicado). Queda como stress-diagnostic para el futuro EXP
de compresión.

## Respuesta a la pregunta central (§1)

**INDETERMINADA POR DATOS — con el diagnóstico disponible, todo apunta a que no es
sólo contaminación:** (i) ninguna señal normal-only predice error ni siquiera dentro
de assets coherentes por el filtro oracle (CIs incluyen 0 en 20°/30°/40°); (ii) la
coherencia global no elimina catastrófes (7 dentro del filtro); (iii) no existe
σ_eff interpretable. PERO la Cohorte A (evidencia principal, §2) no pudo construirse
por bloqueo de red y n=20 no permite cerrar hipótesis (§29). El veredicto definitivo
queda explícitamente pendiente del corpus primario.

## Hypotheses

- H_DATASET_CONTAMINATION: parcialmente refutada como explicación única (B muestra
  fallos impredecibles dentro de coherentes) — no falsificada del todo (n bajo).
- H_NORMAL_ONLY_INFORMATION_LIMIT: consistente con todo lo observado; NO demostrada.
- σ_eff (A) / risk_score (B) de §14: sin soporte en este corpus diagnóstico.

## Mutation sanity

M-equivalentes M3 testeados vía suites M1/M2 intactas (resize renormalizado,
convención, licencia, Q8_XY separado, hf NOT_INFORMATIVE, anti-flatten, sólo-global,
dataset-inválido) + **nuevos**: provenance-gate (MIRROR_ONLY rechazado), hard-stop
§3 (assets<15 y familias<3), regla §27 congelada, tasas exactas en fixture,
bootstrap determinista, anti-circularidad σ_eff (§15), sensibilidad de umbrales
pre-registrada.

## Gates

- EXP-M3 focal: **11/11** nuevos; focal total M0+M1+M2+M3: **70/70** (24+18+17+11).
- ruff check + format --check: clean. mypy: Success. `git diff --check`: ok.
- Full suite (venv reconstruido tras el reset — NO es el baseline de sesiones
  anteriores): **7663 passed / 23 failed / 181 skipped**. Las 23 fallas: **0 en el
  write-set** (la rama no modifica ningún archivo trackeado; sólo agrega 4 archivos
  nuevos) — 15 del lane `runtime_vault` pre-existente desde NP-M0 + 8 de
  `journal`/`project_config` sensibles al entorno (keyring `fail`-backend, tomli-w
  ausente al inicio; corregido parcialmente con pytest-aiohttp/tomli-w). Con el venv
  de M1/M2 el conteo fue 21; el set exacto depende del entorno, nunca del write-set.
- Commit único; race-check pre-push: #617/#618/#620 sin cambios.

## Limitations

Cohorte B hereda la procedencia débil de M2 (mirrors, JPG 8-bit, semántica de height
indocumentada) — por eso su rol es exclusivamente diagnóstico. n pequeño; AUC/ρ con
CIs anchos. El bloqueo de red del sandbox es la limitación dominante y no es técnica
del método.

## Decision

**EXP_M3_DATA_INSUFFICIENT** (§3) — con diagnóstico B que no aporta soporte al
trust automático normal-only y sí evidencia de que la coherencia global es más
esquiva que un escalar. **PRODUCTION sigue NOT READY.** NP-M0 GO · EXP-M1 GO ·
EXP-M2 CONDITIONAL · EXP-M3 DATA_INSUFFICIENT.

## ONE next experiment

**Re-ejecutar EXP-M3 con Cohorte A real** (sin cambiar el protocolo): corpus
primario con upstream-identity verificada — MatSynth (HF) o ambientCG 2K/4K-PNG
descargados fuera del sandbox por el operador, llenando
`exp-m3-clean-authored-manifest.json` con `provenance_status=PRIMARY_VERIFIED` +
SHA256. El pipeline completo (gates, hard-stop, análisis pre-registrado, bandas con
σ_eff independiente) ya corre sin cambios de código. Con ≥15 assets/≥3 familias el
hard-stop se abre solo y la pregunta §1 se responde con evidencia de primera mano.
No iniciarlo desde esta rama.

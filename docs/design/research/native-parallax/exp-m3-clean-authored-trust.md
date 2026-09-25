# EXP-M3 — Clean authored corpus / normal-only trust decision

> Research-only. Sin DDS, BC5 real, Skyrim, MO2, PGPatcher, DynDOLOD, IA ni ParallaxR.
>
> **Estado tras Attempt 2 (corpus primario, 2026-09-23): `EXP_M3_RECONSTRUCTION_CONDITIONAL`
> — el solver funciona en el corpus claro pero está restringido por familia (wood_planks
> mixto, tiles_paving pobre) y el gate de trust §31 NO se cumple en held-out
> (lectura §30: normal-only auto-trust = NO_GO).**
> **Attempt 1 (sandbox) quedó en `EXP_M3_DATA_INSUFFICIENT` y se conserva íntegro al final
> de este documento como histórico.**
> *Nota (Attempt 1): el spec llegó truncado a mitad de §35 ("NO volver a probar SOFT hast…"); se
> siguió hasta allí y se aplicaron las convenciones de M1/M2 para gates/commit/PR.*

## Cohort A Reproduction Run

- **Fecha**: 2026-09-23 (UTC) — corrida en la estación local del operador (Windows, win32).
- **Máquina/entorno**: worktree `C:\Worktrees\Sky-Claw-exp-m3-corpus` en HEAD EXACTO de #621
  (`80d26a7a`, base `e23bf7ac`); venv uv reconstruido (Python 3.11.9, numpy 2.4.6,
  Pillow 11.3.0); `pytest` focal 92/92 antes de la corrida.
- **Dataset manifest**: `data/exp-m3-clean-authored-manifest.json` — **SHA256 de la corrida
  (working tree Windows, CRLF, 76 217 bytes): `c9c1665942281966ddeb4f4ff05ed9e2be302d80155cfa8bfc1e487c2bfbecde`**;
  mismo contenido normalizado a LF (lo que materializa un checkout): `b0f5a4c6604989269647973b6a6e6b899e859b436e7b42bede7e3297d10e6d6f`.
- **Assets/familias/providers**: 31 assets · 8 familias · 2 providers primarios
  (Poly Haven 23 · ambientCG 8). Todo par normal+displacement del MISMO asset id, 1024²
  cuadrado, lossless PNG, convención declarada `OPENGL`, bytes upstream sin transformar.
- **Provenance**: 23 `OFFICIAL_HASH_VERIFIED` (Poly Haven: md5 oficial por archivo publicado
  por la API `files/<id>` y verificado) + 8 `PRIMARY_SOURCE_DOWNLOADED` (ambientCG API v2;
  zip 1K-PNG oficial preservado con sha256; el redirect del CDN usa un token transitorio que
  NO se registra). 0 `MIRROR_*` / `UNKNOWN`. Licencias: CC0-1.0 platform-wide verificada en
  las páginas oficiales (polyhaven.com/license, docs.ambientcg.com/license/). Exclusiones por
  VALIDEZ (no por outcome): `Concrete034` (textura 2:1, aplastada por el resize cuadrado),
  MatSynth (agotamiento acotado: ver metodología), 0 assets eliminados después de correr (§28).
- **Split pre-registrado (§18)**: CALIBRATION 15 (brick 5 · wood_planks 5 · rock 5) /
  HELD_OUT 16 (stone 3 · tiles 4 · concrete 3 · tiles_paving 3 · ground 3). Regla = la de
  EXP-M2 (`CALIBRATION_FAMILIES`), congelada antes de mirar proxies; sin solapamiento
  (test ancla).
- **Convención**: el solver EXP-M2 consume **`DIRECTX`** — calibración independiente del
  outcome: corpus CM del control M2 as-is bueno (Tiles049 0.902), pares `NormalGL` malos
  as-is (Bricks066 0.410→0.979 con flip; Concrete012 0.081→1.000), y variante oficial
  Poly Haven `nor_dx` verificada contra md5 as-is 0.986 vs `nor_gl` 0.428. Por eso todo
  archivo `OPENGL` de Cohort A se convierte al cargar (`SOLVER_NORMAL_CONVENTION`), con
  `tested_convention` auditado por fila.
- **RAW Cohort A (resolución 512)**: N=31 · **mediana |corr| 0.8392** · mediana
  variance_ratio 0.7043 · **8/31 catastróficos** (regla §27 pre-registrada).
  CALIBRATION (15): med corr 0.7716 · var 0.5953 · 4 cat. HELD_OUT (16): med corr 0.8744 ·
  var 0.7647 · 4 cat.
- **Por familia** (mediana |corr| / var / catastróficos / n):
  brick 0.814/0.662/**0**/5 · rock 0.839/0.704/1/5 · wood_planks 0.683/**0.466**/3/5 ·
  stone 0.859/0.738/0/3 · tiles 0.870/0.758/1/4 · concrete 0.821/0.674/1/3 ·
  **tiles_paving 0.547/0.300/2/3** · ground 0.986/0.973/0/3.
- **Normal-only proxies vs outcome (held-out)**: proxy elegido SÓLO en calibración
  (`nz_p01`, ρ_calib −0.400). En held-out: ρ orientado **+0.488**, IC95 **[−0.109, +0.924]**
  (incluye 0), **AUC catastrófico 0.229** (bajo azar), n_cat=4. Direcciones por familia
  inconsistentes (concrete −1.0 · tiles −0.4 vs ground +0.5 · stone +1.0 · paving +1.0).
  Tabla completa de proxies en `runs/attempt-c-valid/exp_m3_results.json`.
- **Rates (held-out, curva §31 sin threshold productivo)**: cobertura auto 12.5%→50% con
  **false-safe 50%→75%**; no existe punto con false-safe 0 dentro del grid. **Nada operativo.**
- **σ_eff independiente (§15/§33)**: bandas `r_p01 = nz_p01/σ_eff` — `curl_mad`: 31/31 en
  `<2`; `projection_median`: 30 en `<2`, 1 en `2-6`, 0 en `>=6` → **NOT_EVALUABLE por
  exceso** (mismo resultado estructural que en B: no hay interpretación útil de r_p01).
- **Cohorte B (comparación)**: reprodujo Attempt 1 exactamente con el corpus M2 re-clonado
  byte-idéntico (34/34 hashes verificados contra el manifest de #620): @20° n=14/4 cat ·
  @30° n=20/7 cat · @40° n=23/8 cat; med corr 0.8944 · var 0.8033 · rmse 0.0404; todos los IC
  de proxies incluyen 0. **No cambia la conclusión.** (Hallazgo colateral: parte del
  «catastrófico» de B con `declared OPENGL` era artefacto de convención no convertida —
  Bricks066 0.41→0.98 con flip — lo que refuerza que B es EVALUATION_DIAGNOSTIC_ONLY y no
  evidencia principal.)
- **Chequeo de orientación de los 8 catastróficos**: ninguna de las 8 transformaciones
  dihedrales de la normal (flips X/Y × swap XY) mejora el resultado — el óptimo es siempre
  la configuración as-loaded post-conversión. No es un artefacto de orientación/convención:
  los pares débiles (`brushed_concrete_03` 0.06, `brown_planks_03` 0.19, `weathered_planks`
  0.20, `precast_stone_paving` 0.34, `Tiles075` 0.40) son **incoherentes de fábrica**
  (sospecha: displacement de Poly Haven derivado de albedo, no de la geometría de la normal).
- **Decisión (enum §29)**: **`EXP_M3_RECONSTRUCTION_CONDITIONAL`** (mapeo pre-registrado:
  RAW viable a nivel corpus 0.839/0.704 ≥ 0.5 pero familias `wood_planks` (var 0.466) y
  `tiles_paving` (0.547/0.300) no viables ⇒ restricción de dominio del solver; §32: no se
  tapa con un trust score).
- **Lectura del gate §31 (respuesta a la pregunta §1)**: **normal-only auto-trust = NO_GO**
  en este corpus: sin generalización held-out (IC incluye 0), AUC bajo azar, direcciones
  familiares inestables y false-safe 50% a coberturas útiles.
- **Consecuencia arquitectónica**: Native Parallax sigue como **generador determinista
  normal→height + métricas de calidad + preview + HITL**, sin auto-trust normal-only;
  aplicabilidad por familia documentada (wood_planks/tiles_paving requieren revisión o
  quedar fuera del modo automático). No se abandona Native Parallax por este veredicto.

## Attempt 2 — corpus primario: metodología y defectos de formato corregidos

Trazabilidad completa de la corrida (los binarios viven fuera del repo, en
`C:\SkyClawResearch\NativeParallax\EXP-M3\`; ~2.1 GB; `originals/` inmutable):

| corrida | directorio | estado | causa raíz demostrada |
|---|---|---|---|
| 2a | `runs/attempt-a-16bit-decode-defect` | NO_GO inválido (corr 0.0) | `decode_height_image` usaba `convert("L")` y **saturaba los PNG de 16 bits a 255** (Poly Haven `I;16`) o los clipeaba (ambientCG). El corpus M2 previo era JPG 8-bit y nunca ejercitó el formato preferido (§7). Corregido con rama explícita 16-bit + tests. |
| 2b | `runs/attempt-b-no-convention` | NO_GO inválido (corr 0.14) | Archivos `NormalGL`/`nor_gl` (convención OPENGL) alimentados as-is a un solver calibrado en DIRECTX; verificado con el control M2 (CM as-is bueno / GL-flip bueno) y con `nor_dx` oficial (md5 verificado). Corregido con `SOLVER_NORMAL_CONVENTION` + conversión auditada. |
| 2c | `runs/attempt-c-valid` | **válido** | Corrida AS-IS del experimento pre-registrado (regla catastrófica, proxies, cohortes, bandas y split sin retuning). |

Adquisición (disciplina §12): APIs oficiales, sin scraping, secuencial con pausas; Poly Haven
`files/<id>` (md5 oficial) + ambientCG API v2 / `get?file=` (zip 1K-PNG). MatSynth: intento
acotado — se descargó y verificó por sha256 LFS un shard test completo (1.14 GB) y se inspeccionó
su metadata; se excluyó del corpus porque (i) la distribución es parquet de 440 GB con viewer
deshabilitado, (ii) el split mezcla CC0 con **CC BY-NC-ND 2.0** (no admisible) y (iii) los
materiales CC0 del shard son re-hosts de ambientCG/TextureCan/TexturesHeaven, i.e. provenance de
segunda mano para una cohorte que exige fuentes primarias directas. Decisión tomada ANTES de
correr y registrada en `attempt_trail` del manifest.

## Gates (Attempt 2)

- Focal NP-M0+M1+M2+M3+corpus: **92/92 passed** (el conteo sube de 70 por los tests nuevos de
  schema/decisión/convención/decoder; collect-only registrado antes de la corrida).
- `ruff check sky_claw/ tests/`: clean · `ruff format --check sky_claw/ tests/`: clean.
- `mypy sky_claw/ --ignore-missing-imports`: Success (0 errores; los módulos de research NO
  están en la lista `ignore_errors`, así que salen type-checked).
- `git diff --check`: clean.
- Full suite (venv reconstruido de esta estación — no es el baseline de sesiones previas):
  **7793 passed / 96 skipped / 0 failed / 0 errors** (10:39). **Fallos en el write-set: 0**.
  Diferencia con el histórico (7663/23/181) atribuible al entorno reconstruido + 22 tests
  nuevos del write-set; no se afirma identidad de node IDs contra baselines previos.

## ONE next experiment (Attempt 2)

**EXP-M4 — techo del solver vs coherencia del par autorado en las familias restringidas.**
Con Cohort A y el split ya congelados: (1) reconstruir desde la derivada espectral del height
authored (mismo `integrate_periodic`, mismas métricas) para fijar el techo alcanzable por asset;
(2) comparar ese techo contra la reconstrucción desde la normal para separar «límite del solver»
de «par autorado incoherente» (p.ej. PH `brown_planks_03`/`weathered_planks`/`brushed_concrete_03`
quedan <0.2 aun con la convención corregida); (3) verificar en documentación del proveedor si el
displacement es derivado de albedo. Sin features nuevas de trust, pre-registrando métricas antes
de correr. No iniciarlo desde esta rama.

## Attempt 1 — DATA_INSUFFICIENT (sandbox, 2026-09-22) — histórico sin cambios

### Repository/base

Verificado contra GitHub (§4, sin SHAs recordados): `MAIN_SHA=3c9b134` · **#617 OPEN
`14b23b0`** · **#618 OPEN `e2a449b`** · **#620 OPEN `e23bf7a`** (base = rama de #618) —
ancestría verificada; rama nueva `research/native-parallax-exp-m3-clean-authored-trust`
desde el head EXACTO de #620. Segundo reset del sandbox entre sesiones (re-clone
shallow @ `30439a1`): working tree verificado byte-a-byte contra el árbol de `e23bf7a`
(33/33 archivos idénticos), estado ajeno preservado en `stash@{0}`, nada reescrito.

### Dataset provenance — intento y HARD STOP

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

### Cohorte A — CLEAN_BY_PROVENANCE

Vacía por fuerza mayor de red. El contrato de procedencia está implementado y
testeado (`load_cohort_a`: sólo `PRIMARY_VERIFIED`/`OFFICIAL_HASH_VERIFIED`; campos
obligatorios provider/official_source/sha256/licencia). El manifest
`data/exp-m3-clean-authored-manifest.json` registra el rastro de intentos y el hash
del propio manifest (`4ee2ffa2…`). Re-ejecutable sin cambios de código cuando haya
fuente primaria: basta llenar `cohort_a.assets`.

### Cohorte B — EVALUATION_DIAGNOSTIC_ONLY (construcción)

Filtro pre-registrado: `height_std ≥ 2/255` (no-plano) Y `oracle_agreement < {20,30,40}°`
(sensibilidad §12; 30° NO normativo). El height se usó SÓLO para pertenencia a la
cohorte; ninguna feature lo recibe (mismas APIs §13 de M2). Etiqueta obligatoria en
cada fila. Procedencia M2 = MIRROR_ONLY (explícitamente NO evidencia principal).

| filtro | n | familias | catastróficos |
|---|---:|---:|---:|
| 20° | 14 | 7 | 4 |
| 30° | 20 | 10 | 7 |
| 40° | 23 | 11 | 8 |

### RAW baseline (dentro de B@30°)

med rmse 0.0404 · med var_ratio 0.803 · med corr 0.894. El núcleo sigue siendo real
en coherentes: brick/rock/snow/tiles con var 0.81–1.00 (consistente con M2).

### Coherence characterization — el hallazgo incómodo

Los **7 catastróficos de B@30° tienen agreement 7°–27°** — pasan el filtro de
coherencia global angular y AUN así son catastróficos por la regla de estructura
(Bricks066 corr 0.41/seam; Concrete012 corr 0.08; Ground037 corr 0.008; Road001
0.23; Metal062C 0.03; Planks012 0.64/var 0.41; WoodFloor030 0.51/0.26). La
coherencia angular global (mediana con strength global fitada) **no garantiza
identidad estructural** banda a banda: el "mismatch artístico" es más sutil que un
escalar. Esto debilita la hipótesis de contaminación simple.

### Pre-registración (§27, antes de mirar proxies en B)

`CATASTROPHIC_RECONSTRUCTION := |corr| < 0.5 OR variance_ratio < 0.5`
(invariante a escala authored; var con escala afín compartida del RAW del asset).
Regla fija en `CATASTROPHIC_RULE`, congelada por test.

### Proxies vs outcome (B@30°; Spearman [IC95 bootstrap], AUC cat)

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

### RATES (§31, curva, sin threshold productivo)

Mejor proxy operable (nz_min): para FALSE_SAFE=0% hay que rechazar el **90%** del
corpus (auto=10%, false_review 61%); con auto=40%: FALSE_SAFE 14% y 50% revisado.
(quantization_proxy resultó degenerado ≡1.0 en JPG — el selector de la curva ahora
excluye proxies sin varianza; corregido y anotado.) **Nada operativo.**

### σ_eff independiente y bandas {2,6} (§33/§34/§15)

Con σ_eff = curl_mad o projection_median (independientes de nz por construcción,
garantizado por test): **r_p01 < 2 para 20/20 assets** — como escala de ruido ambos
residuos superan al percentile nz en órdenes de magnitud; todo el corpus caería en
REJECT, absurdo (7/20 lo son). Las bandas {2,6} vuelven a quedar **NOT_EVALUABLE**,
ahora por exceso y no por circularidad. Conclusión §14: ni la interpretación A
(σ_eff estable) ni la B (risk_score simple) reciben soporte en B; no se conserva la
forma nz/σ por razones arquitectónicas.

### TWO_CHANNEL_Q8_PROXY (§18)

Contadores separados mantenidos (radicando inválido / z=0 / nz<0 real); sin análisis
nuevo en B (sin.encoding aplicado). Queda como stress-diagnostic para el futuro EXP
de compresión.

### Respuesta a la pregunta central (§1)

**INDETERMINADA POR DATOS — con el diagnóstico disponible, todo apunta a que no es
sólo contaminación:** (i) ninguna señal normal-only predice error ni siquiera dentro
de assets coherentes por el filtro oracle (CIs incluyen 0 en 20°/30°/40°); (ii) la
coherencia global no elimina catastrófes (7 dentro del filtro); (iii) no existe
σ_eff interpretable. PERO la Cohorte A (evidencia principal, §2) no pudo construirse
por bloqueo de red y n=20 no permite cerrar hipótesis (§29). El veredicto definitivo
queda explícitamente pendiente del corpus primario.

### Hypotheses

- H_DATASET_CONTAMINATION: parcialmente refutada como explicación única (B muestra
  fallos impredecibles dentro de coherentes) — no falsificada del todo (n bajo).
- H_NORMAL_ONLY_INFORMATION_LIMIT: consistente con todo lo observado; NO demostrada.
- σ_eff (A) / risk_score (B) de §14: sin soporte en este corpus diagnóstico.

### Mutation sanity

M-equivalentes M3 testeados vía suites M1/M2 intactas (resize renormalizado,
convención, licencia, Q8_XY separado, hf NOT_INFORMATIVE, anti-flatten, sólo-global,
dataset-inválido) + **nuevos**: provenance-gate (MIRROR_ONLY rechazado), hard-stop
§3 (assets<15 y familias<3), regla §27 congelada, tasas exactas en fixture,
bootstrap determinista, anti-circularidad σ_eff (§15), sensibilidad de umbrales
pre-registrada.

### Gates

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

### Limitations

Cohorte B hereda la procedencia débil de M2 (mirrors, JPG 8-bit, semántica de height
indocumentada) — por eso su rol es exclusivamente diagnóstico. n pequeño; AUC/ρ con
CIs anchos. El bloqueo de red del sandbox es la limitación dominante y no es técnica
del método.

### Decision

**EXP_M3_DATA_INSUFFICIENT** (§3) — con diagnóstico B que no aporta soporte al
trust automático normal-only y sí evidencia de que la coherencia global es más
esquiva que un escalar. **PRODUCTION sigue NOT READY.** NP-M0 GO · EXP-M1 GO ·
EXP-M2 CONDITIONAL · EXP-M3 DATA_INSUFFICIENT.

### ONE next experiment

**Re-ejecutar EXP-M3 con Cohorte A real** (sin cambiar el protocolo): corpus
primario con upstream-identity verificada — MatSynth (HF) o ambientCG 2K/4K-PNG
descargados fuera del sandbox por el operador, llenando
`exp-m3-clean-authored-manifest.json` con `provenance_status=PRIMARY_VERIFIED` +
SHA256. El pipeline completo (gates, hard-stop, análisis pre-registrado, bandas con
σ_eff independiente) ya corre sin cambios de código. Con ≥15 assets/≥3 familias el
hard-stop se abre solo y la pregunta §1 se responde con evidencia de primera mano.
No iniciarlo desde esta rama.

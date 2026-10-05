# REVAL-0 — Protocolo congelado de revalidación matemática M2/M3

**Versión:** REVAL-0 · **Fecha del freeze documental:** 2026-10-05 (UTC)
**Base auditada:** `origin/main = 0c2c5414605c2892b646f02cd31a8930748f9e8a`
**Alcance:** protocolo de comparación histórica `OLD` vs. código matemático/estadístico corregido `NEW`; no ejecución.

> **NOTA DE PROTOCOLO (RECOVERY-0 INTEGRADO):** este documento congela cómo comparar; no ejecuta M2/M3 ni autoriza una sustitución de corpus. Tras la auditoría RECOVERY-0 en el host Windows, el corpus M2 histórico (34/34 assets, 68/68 archivos con SHA-256 verificado), el corpus M3 (31 assets, 62 archivos con SHA-256 verificado), el manifiesto local de M2, y los artefactos históricos `rows.json` (174 filas) y `characs.json` (34 assets) fueron localizados y autenticados con evidencia convergente fuerte. Por tanto, el bloqueo de procedencia queda resuelto y la ejecución futura queda técnicamente habilitada bajo el protocolo (`REVALIDATION_EXECUTION_ALLOWED=YES`), pero ninguna ejecución real (M2, M3 ni M6) se ejecuta en esta fase protocolar.

```text
PROTOCOL_CLASS=VERSIONED_REVALIDATION_OF_PREVIOUSLY_OBSERVED_DATA
REVAL_PROTOCOL_COMPLETE=YES
REAL_CORPUS_TOUCHED=NO
M2_RERUN_EXECUTED=NO
M3_RERUN_EXECUTED=NO
RECOVERY_BLOCKER=NONE
REVALIDATION_EXECUTION_ALLOWED=YES
```

## 1. Pregunta y límites científicos

Pregunta única:

> ¿Qué resultados históricos de M2/M3 cambian cuando se ejecutan los mismos corpus y protocolos con las primitivas matemáticas y estadísticas corregidas?

No se pregunta si se pueden mejorar los resultados retuneando M2/M3. No se optimiza, calibra de nuevo, busca una nueva política ni se selecciona un corpus alternativo.

Todo el corpus fue observado históricamente. Esta fase es `VERSIONED_REVALIDATION_OF_PREVIOUSLY_OBSERVED_DATA`, **no** una confirmación ciega nueva ni una confirmación fresh held-out. Se conservan los splits históricos exclusivamente para comparabilidad; `CALIBRATION` y `HELD_OUT` ya no son blind.

Las correcciones MATH-A, MATH-B y REPRO-A se consideran cerradas y no se reabren en REVAL-0. M4/M5 no se rerunean ni se reescriben aquí. Los estados existentes se conservan:

```text
M4_PRIMARY_INVALIDATED_BY_CURRENT_EVIDENCE=NO
M5_PRIMARY_INVALIDATED_BY_CURRENT_EVIDENCE=NO
```

No modificar el PR #675. En la auditoría de este freeze se observó `OPEN` + `DRAFT`; el estado requerido sigue siendo:

```text
M6_IMPLEMENTATION_BLOCKED=YES
M6_REAL_RUN_EXECUTED=NO
```

## 2. Baseline y auditoría de referencias históricas

### 2.1 Baseline del repositorio

Antes de trabajar se ejecutó `git fetch origin --prune`, se comprobó `git rev-parse origin/main` y `git status --short`. Se auditó la sincronización con `origin/main = 0c2c5414605c2892b646f02cd31a8930748f9e8a` (incorporando PR #688 en documentación externa sin conflicto). Se auditó el estado presente de:

```text
sky_claw/local/native_parallax/**
tests/test_native_parallax_*
docs/design/research/native-parallax/**
docs/validation/**
```

No se detectó colisión ni movimiento en el dominio de Native Parallax (`NATIVE_PARALLAX_BASE_OVERLAP=NO`, `NATIVE_PARALLAX_BASE_MOVED_MATERIALLY=NO`).

### 2.2 M2 — resultado/documento, manifiesto, filas y corpus

**Referencia histórica encontrada:**

- Documento inmutable: `docs/design/research/native-parallax/exp-m2-authored-trust.md`.
- Enum histórico declarado: `EXP_M2_CONDITIONAL`.
- El documento fija el baseline M2 principal en 512²; documenta además un subset de sensibilidad 1024², que no pasa a ser resultado principal.
- El documento contiene tablas históricas —incluida una tabla RAW por asset— y resultados agregados. Son referencias `OLD`; no se copiarán como resultados `NEW` ni se reconstruirán los artefactos faltantes a partir de prosa/tablas.
- El documento indica `MAIN_SHA=607ff21`. No existe atestación 40-hex demostrada ni verificada en Git o en los artefactos de ese execution SHA (`OLD_M2_EXECUTION_SHA_FULL_VERIFIED=NO`, `OLD_M2_EXECUTION_SHA_PREFIX=607ff21`). Queda prohibido expandir o inferir `607ff21` a un SHA completo de 40 hex. El comparador y sidecar de metadatos NEW debe registrar literalmente una semántica equivalente a:
  ```json
  {
    "old_execution_commit": {
      "value": "607ff21",
      "kind": "historical_7_hex_prefix",
      "full_40_hex_verified": false
    }
  }
  ```
  La ejecución NEW sí debe registrar obligatoriamente `RUN_CODE_SHA=<40 hex exacto>`. Se documentan además en el histórico Python 3.11.2, NumPy 2.4.6, Pillow 12.3.0 y Linux 6.1.

**Manifiesto canónico localizado en Git:**

```text
path=docs/design/research/native-parallax/data/exp-m2-authored-manifest.json
sha256=4d00501949fc006d39a10ba148b269a35c080a92c9e367a4d5a2b7b029d941cb
bytes=36501
assets=34
split=CALIBRATION 12 / HELD_OUT 22
```

Contiene hashes SHA-256 esperados para normal y height por asset, convenciones declaradas y split. El documento histórico cita el mismo prefijo `4d00501949fc006d`. Esto acredita la presencia del manifiesto versionado en el repo, **no** la identidad de los archivos externos ni la identidad byte-a-byte del manifiesto local usado en la corrida histórica.

**Filas/caracterizaciones históricas (RECOVERY-0):**
Se recuperaron los artefactos originales bajo `C:\SkyClawResearch\NativeParallax\EXP-M3\m2_control\`:
- `rows.json`: 254,405 bytes, SHA-256 `c2d8328e62046245d4c395ee0ecaad3696b8fb48f6578c9582079182ad770a08`. Contiene exactamente las 174 filas generadas por `run_exp_m2.py` (34 RAW, 68 transfer k=1, 72 sweep k en {0.5, 2.0, 4.0} sobre las 12 familias de CALIBRATION) y la selección de sigma histórico con ganador `nz_p01`.
- `characs.json`: 91,796 bytes, SHA-256 `9afb60a453d2783572145c3d270145b2dc668941bbbdf96a5ac25cf7cef7a683`. Contiene las caracterizaciones completas de los 34 assets emitidas por `characterize_asset()`.

**Procedencia y autenticación de M2 OLD:**
La autenticación se sustenta en evidencia convergente sólida: rutas históricas esperadas, timestamps coherentes (`2026-09-23 21:30:07 UTC`), estructura exacta del runner `run_exp_m2.py`, 34 assets, 174 filas, ganador sigma histórico `nz_p01`, métricas agregadas que coinciden exactamente con `docs/design/research/native-parallax/exp-m2-authored-trust.md` (mediana RMSE RAW 0.0434, p90 0.1033, peor 0.1743, mediana var 0.631, mediana corr 0.794, 15/34 catastróficos), y diagnósticos angulares del oráculo por asset (Grass004 88.94°, Gravel011 51.39°, WoodFloor027 81.46°, Wood043 66.37°, Metal063 64.82°).
Clasificación: `M2_OLD_ARTIFACTS_VERIFIED=YES`. Formulación rigurosa: *Authenticated with strong convergent provenance and exact corpus/hash identity; sufficient for versioned revalidation* (sin afirmar certeza matemática absoluta o prueba criptográfica de ejecución histórica, al no existir una firma publicada contemporánea).

**Corpus y manifiesto local histórico (RECOVERY-0):**

- Manifiesto local recuperado: `C:\SkyClawResearch\NativeParallax\EXP-M3\cohort_b\exp-m2-authored-manifest-local.json` (40,148 bytes, SHA-256 `40ff24bd3342c60d6d23d700975ae5d4f37adffc96bb900aaaf0ecea58d5a34a`).
  - Comparación con manifest canónico del repo: 34 asset IDs idénticos, hashes SHA-256 por archivo para normal y height idénticos (68/68), split idéntico (12/22), convenciones declaradas idénticas (24 UNKNOWN / 10 OPENGL), provider/mirror idénticos.
  - Diferencia de rutas: rutas absolutas de Windows en el manifiesto local (`C:\SkyClawResearch\NativeParallax\EXP-M3\cohort_b_mirrors\...`) vs. rutas canónicas/sandbox (`/tmp/expm2_data/mirrors/...`).
  - Clasificación: `SEMANTICALLY_EQUIVALENT_MANIFEST=YES`, `BYTE_IDENTICAL_MANIFEST=NO`, `EXPECTED_PATH_PREFIX_DIFFERENCE=YES` (no son byte-idénticos por prefijo de ruta).
- Corpus M2 auditado:
  - Raíz física: `C:\SkyClawResearch\NativeParallax\EXP-M3\cohort_b_mirrors`.
  - Conteo: 34 assets, 68 archivos esperados (34 normal + 34 height), 68 archivos encontrados.
  - Coincidencia de hashes: 68/68 archivos con coincidencia exacta SHA-256 (`M2_FILES_SHA256_MATCH=68`, `M2_FILES_SHA256_MISMATCH=0`).
  - Clasificación: `M2_CORPUS_IDENTITY_VERIFIED=YES`.
- Junction histórico:
  - Localizado e intacto: `E:\tmp\expm2_data\mirrors` (NTFS directory junction hacia `C:\SkyClawResearch\NativeParallax\EXP-M3\cohort_b_mirrors`). Registrado como procedencia auxiliar (la prueba primaria de identidad son los hashes por archivo).

Estado de auditoría M2 a la fecha:

```text
M2_HISTORICAL_REFERENCE_LOCATED=YES
M2_CANONICAL_MANIFEST_LOCATED=YES
M2_HISTORICAL_LOCAL_MANIFEST_LOCATED=YES
M2_HISTORICAL_ROWS_LOCATED=YES
M2_HISTORICAL_CHARACS_LOCATED=YES
M2_CORPUS_AVAILABLE=YES
M2_CORPUS_IDENTITY_VERIFIED=YES
M2_OLD_ARTIFACTS_VERIFIED=YES
```

### 2.3 M3 — artefacto histórico y manifiesto

**Referencias inmutables localizadas:**

- Documento: `docs/design/research/native-parallax/exp-m3-clean-authored-trust.md`.
- Resultado publicado: `docs/validation/native-parallax-revalidation-20260928/exp_m3_results.json`.
- Checkpoint/procedencia: `docs/validation/native-parallax-revalidation-checkpoint.md`.

El JSON publicado tiene SHA-256 `02f6d1ac5a679081359b43a173f8e849fa9819e0d1c4e60830266a337b2335da`, 237549 bytes (blob/publicación LF). El checkpoint registra que el output RAW externo de Windows era CRLF, SHA-256 `047769fd…` y 243752 bytes; la copia publicada corresponde a normalización de EOL, no a una segunda corrida. En la auditoría RECOVERY-0 se recuperó el archivo RAW original en disco (`C:\SkyClawResearch\NativeParallax\EXP-M3\runs\revalidation-height-resize-20260928\m3\exp_m3_results.json`, 243,752 bytes, CRLF, SHA-256 `047769fdad05e9af7960feb2c414470af7493d847e0901bac51d7c4a84bc5e19`). Se verificó que `RAW CRLF != published LF bytewise` pero `JSON semantic equality = TRUE`. Los artifacts históricos se preservan sin modificación.

El manifiesto primario M3 versionado es:

```text
path=docs/design/research/native-parallax/data/exp-m3-clean-authored-manifest.json
sha256 LF= b0f5a4c6604989269647973b6a6e6b899e859b436e7b42bede7e3297d10e6d6f
bytes LF=74784
```

El resultado M3 registra `cohort_a.manifest_sha256=c9c1665942281966ddeb4f4ff05ed9e2be302d80155cfa8bfc1e487c2bfbecde`, que el documento/checkpoint identifica como el mismo contenido en el checkout Windows CRLF (76217 bytes), normalizado a LF como el manifiesto versionado actual. Esta relación de EOL se conserva explícita; no se deben comparar esos dos SHA como si fueran bytes idénticos.

El artefacto `OLD` registra: Cohort A usable/evaluated 31/31, 0 rechazados y 0 dataset-invalid; decisión `EXP_M3_RECONSTRUCTION_CONDITIONAL`; RAW mediana `median_abs_corr=0.8391782269373411`, `median_variance_ratio=0.7042200965657`; CALIBRATION n=15, proxy `nz_p01`, Spearman `-0.39999999999999997`, orientación `-1.0`; HELD_OUT n=16, Spearman `+0.488235294117647`, IC95 `[-0.1088970588235294, +0.9235294117647058]`, AUC catastrophic `0.22916666666666666`, 4 catastróficos. La tabla de M3 del checkpoint contiene también las referencias aproximadas del brief. **Todo lo anterior es OLD/reference, nunca NEW.**

El JSON histórico no contiene `n_boot_evaluable` ni `n_boot_degenerate`; su ausencia significa `NOT_RECORDED`, no cero. No rellenar retrospectivamente esos campos en el artefacto OLD. Los nuevos campos se reportarán en el artefacto NEW; si un recálculo de OLD desde sus vectores publicados se llegara a proponer, se separará como cálculo derivado, no como dato del output RAW histórico, y requerirá revisión/adjudicación antes de interpretarse.

El checkpoint registra para la corrida histórica un baseline corto `1a52c3ea`; el JSON de resultados no incluye `RUN_CODE_SHA` ni environment. La presencia de un SHA de freeze en la sección M4 del checkpoint no se presenta como atestación directa del commit de ejecución M3. Mantener esa limitación de procedencia visible; no inventar un SHA histórico.

**Corpus Cohort A recuperado y auditado (RECOVERY-0):**
El corpus externo fue localizado en `C:\SkyClawResearch\NativeParallax\EXP-M3\originals` (directorios `ambientcg` y `polyhaven`). Se auditaron criptográficamente todos los archivos correspondientes a los 31 assets de Cohort A (15 CALIBRATION, 16 HELD_OUT):
- Archivos esperados: 62 (31 pares normal + height).
- Archivos encontrados: 62.
- Coincidencia de hashes SHA-256: 62/62 (`M3_FILES_SHA256_MATCH=62`, `M3_FILES_SHA256_MISMATCH=0`).
- Clasificación: `M3_CORPUS_IDENTITY_VERIFIED=YES`.

Cohort B está etiquetada `EVALUATION_DIAGNOSTIC_ONLY`; el resultado histórico registra sus thresholds 20°, 30° y 40° (n=14/20/23, respectivamente) y filas/features, y su corpus y manifiesto local han sido recuperados y autenticados íntegramente como se documenta en §2.2.

```text
M3_HISTORICAL_REFERENCE_LOCATED=YES
M3_PUBLISHED_RESULT_LOCATED=YES
M3_HISTORICAL_RAW_LOCATED=YES
M3_MANIFEST_LOCATED=YES
M3_CORPUS_IDENTITY_VERIFIED=YES
```

## 3. Protocolo de corpus, split y resolución

El contraste causal es válido sólo si OLD y NEW usan:

```text
same assets
same normal/height bytes (cada SHA-256 por asset)
same manifest semantics and declared conventions
same family/asset split
same exclusions and validity rules
same primary resolution
same thresholds, seeds, policies and candidate definitions
```

1. La identidad se valida por asset ID, rol (normal/height), hash SHA-256 esperado y hash observado. Registrar por separado hash del manifiesto histórico/raw, hash del manifiesto usado en NEW y cualquier cambio de ruta. Un cambio de ruta sólo es una reubicación si una auditoría documentada demuestra que todos los bytes y la semántica son los mismos; la ruta no prueba identidad.
2. Está prohibido sustituir un asset faltante, cambiar proveedor/mirror, recrear el corpus con una descarga nueva o aceptar sólo un subset “parecido”. Cualquier hash ausente o mismatch produce `CORPUS_IDENTITY_FAILURE` y detiene la ejecución/interpretación.
3. La ruta absoluta puede ser local al entorno, pero no se cambiarán asset IDs, split, convenciones declaradas, semántica de height, filtros ni reglas. Conservar los bytes del manifiesto recibido y sus hashes; no sobrescribir manifiestos históricos.
4. Resolución primaria única: **512**. 1024 no se eleva a principal. Un control/subset 1024 sólo puede repetirse si era parte del protocolo histórico correspondiente y se recupera su lista exacta de IDs, reglas y referencias OLD. No crear un nuevo resultado 1024.
5. Mantener el split histórico, pero rotularlo explícitamente como ya observado/no blind. Ningún informe debe decir “new blind confirmation”, “fresh held-out” o equivalente.

## 4. Parámetros que no se retunean

Los siguientes valores y reglas se conservan exactamente como en los protocolos/runners históricos actuales; no se modifican a la vista de resultados.

### 4.1 M2

- `RES_MAIN=512`.
- Split por familias: CALIBRATION = `brick`, `wood_planks`, `rock`; HELD_OUT = resto.
- Catastrófico: `aligned_rmse > 0.25 OR variance_ratio < 0.5`.
- `K_TRANSFER=1.0`; sweep M2-F sólo CALIBRATION, `K_SWEEP=(0.5, 2.0, 4.0)`.
- Conjunto de candidatos sigma, sin agregar/eliminar/renombrar: `curl_mad`, `projection_median`, `nz_p05_div3`, `nz_p01`.
- Tabla sigma predefinida: cada candidato contra `aligned_rmse` y `gradient_rmse` en CALIBRATION. La selección usa el Spearman de `aligned_rmse` y sólo valores evaluables. No cambiar el criterio, dirección, split ni desempate.
- Si ningún candidato es evaluable: conservar `winner=None`, `status=NO_EVALUABLE_SIGMA_CANDIDATE` y no ejecutar filas M2-E/F dependientes de sigma. No resucitar el fallback histórico ni elegir un candidato a mano.
- RAW y todas las políticas/campos se reportan por fila, sin crear una única métrica resumen post-hoc.
- El control 1024 documentado para un subset M2 queda fuera del resultado principal y sólo es admisible con el subset histórico exacto y su old artifact recuperado.

### 4.2 M3

- Primary = Cohort A con el manifest primario existente; Cohort A exige al menos 15 assets y 3 familias y sólo acepta estados de provenance ya permitidos por el runner (`PRIMARY_SOURCE_DOWNLOADED`, `OFFICIAL_HASH_VERIFIED`). No se incorporan reemplazos.
- Convención consumida por el solver: `DIRECTX`; conversiones declaradas se mantienen y se auditan por fila.
- Regla catastrófica: `abs(correlation) < 0.5 OR variance_ratio < 0.5`.
- Split CALIBRATION/HELD_OUT histórico, sin cambios.
- Cohort B continúa exclusivamente como `EVALUATION_DIAGNOSTIC_ONLY`; conservar `HEIGHT_FLAT_STD=2/255` y filtros de oracle de 20°, 30° y 40°. 30° no se convierte en umbral normativo nuevo.
- Reglas de decisión Cohort A, sin cambios: RAW mediana |corr| ≥0.5 y variance ratio ≥0.5; familia evaluable n≥3; n mínimo por split 5; `abs(Spearman held-out) ≥0.5`; IC que no incluya 0; AUC catastrófico ≥0.75; al menos 2 catastróficos y 2 no catastróficos en held-out; ≥2 direcciones familiares held-out positivas con n≥3; alguna fila de rates con cobertura auto ≥0.3 y catastrophic false-safe ≤0.
- Bootstrap de Spearman: n=1000, seed=20260922, muestreo con reemplazo, cuantiles 2.5/97.5; cada réplica no evaluable se cuenta como degenerada, no se convierte en un rho fabricado.
- Mantener listas de proxies, umbrales low-trust, bandas sigma independientes y sweep de rates existentes. Para rates, conservar cuantiles `(0.1, 0.2, 0.3, 0.4, 0.5)` y las definiciones actuales. No ajustar ninguna regla para compensar una falta de evaluación.

Si el código de la revisión que se va a ejecutar no cumple estos valores/reglas, detenerse antes del corpus y resolver la divergencia mediante revisión técnica del protocolo, nunca con un cambio silencioso al runner.

## 5. Familias causales y mapa de impacto

Cada delta numérico debe tener un `causal_path` comprobable. No se atribuye a MATH-A/MATH-B sólo porque el número cambió. Para cada métrica diferente, el impact report debe responder explícitamente:

```text
Did it consume curl_proxy?
Did it consume gradients_from_normal?
Did it consume decode_gradients_policy?
Did it consume projection_residual?
Did it consume normal_height_residual_oracle?
which call site / direct or downstream path / sx / sy / policy / sigma?
```

Registrar `YES/NO` por primitive. Si todos los consumidores relevantes son `NO`, o la relación transitiva no se puede demostrar, no atribuir el delta a MATH-A: clasificarlo `UNEXPECTED_CHANGE_INVESTIGATE_BEFORE_INTERPRETATION`.

### A — Sensible a geometría/matemática (MATH-A)

| Primitiva corregida | Consumidores M2/M3 | Cambios candidatos y límite de atribución |
|---|---|---|
| `curl_proxy` / `curl_field` | Features `curl_median_abs`, `curl_mad`, `curl_p95_abs`; candidatos de sigma y tablas/rankings proxy | Los valores curl pueden cambiar. Una consecuencia posterior en `sigma_selection`, proxy selection o policies debe trazarse por asset y candidato; no es automáticamente delta del solver RAW. |
| `projection_residual` | `projection_residual_mean_deg`, `_median_deg`, `_p95_deg`; sigma candidato `projection_median`; M3 proxy/risk/rates | Esperables cambios de feature, rank, selección y diagnósticos que consuman esos campos. Trazar los usos indirectos. |
| `normal_height_residual_oracle` | M2 `oracle_agreement_deg`/`oracle_best_strength`; diagnósticos de Cohort A; filtro de pertenencia M3 Cohort B a 20°/30°/40° | El agreement/strength puede cambiar. La pertenencia/n de Cohort B puede cambiar por el nuevo filtro; sigue siendo DIAGNÓSTICA y no cambia la pertenencia de Cohort A. |
| `gradients_from_normal`, `decode_gradients_policy` y cadena de reconstrucción | M2/M3 RAW y políticas, métricas de reconstrucción/activación | El runner M2/M3 llama estos caminos con `sx=sy=1.0`. La corrección del factor anisotrópico `sx/sy` es algebraicamente invisible en esos valores; por sí sola no predice cambios RAW. Cualquier delta en aligned RMSE, gradient RMSE, correlation, variance o activation exige demostrar qué operación corregida consumió esa fila. |
| Anisotropía `sx != sy` | No forma parte de la configuración M2/M3 congelada (las llamadas primarias usan 1.0/1.0) | No agregar una prueba anisotrópica ni atribuir cambios a ella. Si una fila revela escalas no unitarias, es desviación/protocolo no comparable: detener e investigar. |

### B — Sensible a estadística (MATH-B)

- OLD usaba ranking ordinal/double-`argsort`; NEW usa average ranks para empates. Comparar los vectores de entrada y el ranking, no sólo el rho final.
- Sin empates, OLD y NEW deben coincidir (salvo que el mismo cálculo demuestre una diferencia de precisión reproducible). Una diferencia sin empates es `INVESTIGATE_BEFORE_INTERPRETATION`, no “ruido pequeño”.
- Entrada constante/no evaluable debe producir `NOT_EVALUABLE`, no un coeficiente fallback. M2 puede quedarse sin sigma winner y omitir políticas M2-E/F. En M3, `rho_cal == 0` produce `CALIBRATION_SPEARMAN_ZERO_NO_ORIENTATION`; no se fabrica una orientación `+1/-1` ni un risk score. M3 puede quedarse sin proxy/orientación/rates evaluables y fallar cerrado. Eso no es automáticamente regresión.
- Un cambio posterior en AUC/rates/direcciones puede venir de que cambió el proxy o su orientación por MATH-B; marcarlo como efecto indirecto y mostrar esa cadena, no como cambio de la fórmula de AUC/rates.

### C — Sólo serialización (REPRO-A)

`NaN/+Inf/-Inf → null` en el límite JSON estricto (`allow_nan=False`) es exclusivamente `SERIALIZATION_ONLY_NONFINITE`. No es delta numérico, regresión ni cambio científico. Distinguirlo de un `null` por `NOT_EVALUABLE` estadístico y conservar el estado/diagnóstico que explique el `null`.

## 6. Comparaciones primarias requeridas

No se permite reemplazar estas tablas por una puntuación compuesta elegida después de ver los datos.

### 6.1 M2 — `rows.json` + `characs.json`

Comparar, por ID de asset y donde corresponda por policy/k:

1. Conteo de assets de entrada, incluidos, excluidos/rechazados, razón y split; pertenencia de ID y declared convention.
2. Todas las filas RAW 512 y campos asociados, incluyendo `aligned_rmse`, `raw_centered_rmse`, `gradient_rmse`, `correlation`, `variance_ratio`, `catastrophic`, `activation_fraction`, `negative_nz_fraction`, `low_trust_fraction`, métricas de gradiente, seams, energy ratio, escala/signo del oráculo y runtime (runtime se reporta, no se interpreta como efecto científico sin separar environment).
3. `characs.json` por asset: features normal-only, estadísticas curl/projection, `oracle_agreement_deg`, `oracle_best_strength`, seam/Q8 y diagnósticos de convención/orientación disponibles.
4. Tabla sigma completa: cada `candidate × target`, Spearman, sigma median y elegibilidad; ganador, estado de selección, y cualquier policy row generado o no generado.
5. Todas las filas transfer M2-E y sweep M2-F, policy/k/sigma por asset, incluyendo `aligned_rmse`, `gradient_rmse`, `correlation`, `variance_ratio`, `catastrophic` y `activation_fraction`. Si NEW no tiene winner, registrar ausencia de esas filas como consecuencia fail-closed; no inventarlas.
6. Si y sólo si se recupera el control M2 1024 histórico, comparar exactamente su subset y separarlo del primary 512.

Las filas se alinean por claves semánticas (asset, policy, k, sigma/candidato y split), nunca por posición del array. Si no se recuperan los OLD raw `rows.json`/`characs.json`, M2 no tiene comparación completa y no se ejecuta.

### 6.2 M3 — `exp_m3_results.json`

1. Enum final `decision`; `DECISION_EQUIVALENT` sólo si el enum NEW es **exactamente** igual al enum OLD. Medianas similares no bastan.
2. Cohort A: usable, rejected, evaluated, dataset-invalid, razones/IDs y split.
3. RAW Cohort A: `median_abs_corr`, `median_variance_ratio`; resúmenes por CALIBRATION/HELD_OUT y por familia (n, split, catastróficos, ambas medianas); conservar también las filas y features per asset.
4. CALIBRATION: proxy seleccionado, n, Spearman, orientación y status; tabla completa de proxy/ranking y estado de evaluabilidad.
5. HELD_OUT: Spearman orientado, IC, n, `ci_status`, `n_boot_evaluable`, `n_boot_degenerate`, catastrophic AUC y n de positivos/negativos. Si OLD no tiene conteos bootstrap, reportar `NOT_RECORDED`; no inferir cero.
6. Direcciones por familia (n, Spearman, status) y la tabla rates completa (proxy, threshold, coverage, reject, catastrophic false-safe y false-review). Registrar también si el proxy/rates no fue evaluable.
7. Cohort B DIAGNÓSTICA: a cada umbral histórico 20°/30°/40°, IDs incluidos, n, familias, catastróficos, features, filas y proxies; cambios de membresía atribuibles al oracle deben quedar visibles. No usar Cohort B para cambiar la decisión primaria de Cohort A.
8. `bands_independent_sigma`, `by_threshold`, diagnósticos de convención/oracle y cualquier otro campo preexistente se conserva/compara cuando esté presente. No omitir un campo porque NEW sea `null` o porque OLD no tuviera ese campo.

## 7. Contrato de equivalencia y del comparador

El comparador será una herramienta de investigación separada (no parte del runner, no implementada en REVAL-0). Comparará objetos/filas semánticamente y emitirá para cada campo:

```json
{
  "path": "#/cohort_a/summary/heldout_trust/spearman",
  "old_value": 0.488235294117647,
  "new_value": null,
  "delta": null,
  "abs_delta": null,
  "rel_delta": null,
  "bit_identical": null,
  "classification": "HISTORICAL_VALUE_WAS_NOT_STATISTICALLY_EVALUABLE",
  "causal_path": ["..."],
  "notes": "..."
}
```

Contrato numérico:

- Para dos números finitos: registrar OLD, NEW, `delta = NEW − OLD`, `abs_delta = abs(delta)`, `rel_delta = delta/abs(OLD)` sólo si OLD ≠ 0, y `bit_identical` comparando la representación IEEE-754 binary64 tras parsear el número JSON (los runners serializan floats que round-trip). No usar tolerancia científica post-hoc.
- Si uno es cero, `rel_delta=null` con razón; si un valor es no finito/null/missing/categórico, delta relativo no aplica. Conservar el token no finito OLD como tal al parsear JSON histórico no estricto; no normalizar el archivo OLD.
- Reportar **todo** delta numérico, aunque sea pequeño. No fijar umbral `abs(delta)<X` ni declarar cambios “irrelevantes” por tamaño. La magnitud se presenta; la clasificación depende del camino causal y del estado de evaluabilidad.
- Arrays se emparejan por claves de dominio; unmatched IDs/campos son explícitos, no ocultos por diff textual ni descartados.
- Metadatos de procedencia del comparador/sidecar: registrar explícitamente el commit de ejecución de cada lado. Para M2 OLD, registrar `OLD_M2_EXECUTION_SHA_FULL_VERIFIED=NO` y `OLD_M2_EXECUTION_SHA_PREFIX=607ff21` bajo la estructura:
  ```json
  {
    "old_execution_commit": {
      "value": "607ff21",
      "kind": "historical_7_hex_prefix",
      "full_40_hex_verified": false
    }
  }
  ```
  mientras que para la corrida NEW se exige `RUN_CODE_SHA=<40 hex exacto>`.

Clasificaciones permitidas, no mutuamente intercambiables:

```text
UNCHANGED_BIT_IDENTICAL
MATH_A_DIRECT
MATH_A_DOWNSTREAM
MATH_B_TIE_CORRECTION
MATH_B_DOWNSTREAM_SELECTION_OR_ORIENTATION
HISTORICAL_VALUE_WAS_NOT_STATISTICALLY_EVALUABLE
NEW_DIAGNOSTIC_ADDED_BY_MATH_B
POTENTIAL_TOOLCHAIN_FLOATING_NOISE
SERIALIZATION_ONLY_NONFINITE
EXPECTED_DIAGNOSTIC_COHORT_MEMBERSHIP_CHANGE
CORPUS_IDENTITY_FAILURE
UNEXPECTED_CHANGE_INVESTIGATE_BEFORE_INTERPRETATION
```

**Reglas de la clasificación `POTENTIAL_TOOLCHAIN_FLOATING_NOISE`:**
Esta clasificación separa divergencias microscópicas atribuibles a variaciones de toolchain o bibliotecas matemáticas en coma flotante (p. ej. rutinas BLAS/LAPACK o implementaciones FFT entre plataformas y versiones de NumPy/Python).
- **Prohibición estricta de tolerancia relajada:** NO introduce una tolerancia numérica que declare equivalentes los resultados científicos. NO modifica umbrales ni thresholds de decisión. NO redondea valores antes de comparar. Se deben registrar SIEMPRE `old_value`, `new_value`, `delta`, `abs_delta`, `rel_delta` y `bit_identical=false`.
- **Condiciones concurrentes obligatorias:** Sólo puede aplicarse cuando se satisfacen simultáneamente tres criterios:
  1. La magnitud del delta se ubica rigurosamente en la escala de precisión de máquina (`abs(delta) ~ 1e-16` o variación mínima del LSB IEEE-754).
  2. NINGÚN estado categórico, estado de decisión, corte de umbral, selección de proxy/sigma ni dirección familiar se altera como consecuencia del delta.
  3. NINGUNA ruta matemática o estadística corregida por MATH-A o MATH-B explica causalmente la discrepancia.
- **Auditoría obligatoria de entorno:** No se califica automáticamente cualquier diferencia pequeña (`~1e-16`) como toolchain sin verificar las tres condiciones e incluir en el reporte de comparación los metadatos completos de entorno (`OS`, `Python`, `NumPy`, `Pillow`, `FFT backend`).

`DECISION_EQUIVALENT=YES` sólo cuando `OLD.decision == NEW.decision` como enum literal exacto. Si difiere, `DECISION_EQUIVALENT=NO`; reportar cada condición del enum que cambió y su camino causal. No concluir equivalencia por medianas, dirección general o parecido narrativo.

Un `null` NEW por constante, rho=0 sin orientación, sigma winner=None, bootstrap no evaluable o ausencia de proxy no se llama automáticamente regresión: cuando aplique se clasifica `HISTORICAL_VALUE_WAS_NOT_STATISTICALLY_EVALUABLE`, y se explica el estado. Un null exclusivamente originado por saneamiento JSON se clasifica `SERIALIZATION_ONLY_NONFINITE`.

## 8. Diagnósticos obligatorios de empates Spearman

Para **cada** Spearman OLD/NEW que no sea bit-identical, guardar un registro de auditoría con:

```text
path
n_old, n_new, n_paired
OLD_RANKING_METHOD=ORDINAL_DOUBLE_ARGSORT | UNKNOWN
NEW_RANKING_METHOD=AVERAGE_RANK
TIES_PRESENT_OLD=YES/NO
TIES_PRESENT_NEW=YES/NO
number_of_tie_groups_x_old, number_of_tie_groups_y_old
max_tie_size_x_old, max_tie_size_y_old
number_of_tie_groups_x_new, number_of_tie_groups_y_new
max_tie_size_x_new, max_tie_size_y_new
OLD_RHO, NEW_RHO, DELTA, ABS_DELTA, REL_DELTA, BIT_IDENTICAL
```

Un tie group es un valor repetido al menos dos veces, contado después de documentar el mismo filtro de pares válidos usado para ese rho; reportar además los valores/pares excluidos si los hay. La implementación OLD se confirma contra el código histórico disponible; si no se puede verificar, registrar `UNKNOWN`, nunca asumirlo. NEW debe ser average ranks. Si no hay ties en OLD ni NEW, el resultado esperado es OLD==NEW; cualquier diferencia se investiga antes de interpretación, incluso si parece pequeña. No inventar `n`, empates ni grupos a partir del rho agregado.

## 9. Identidad de código, environment y artefactos

Antes del run futuro REVAL-1, después de los gates de review y freeze:

- Ejecutar desde el commit exacto de `origin/main` aprobado para el run, con árbol limpio; registrar `RUN_CODE_SHA=<40-hex exacto>` en README/comparison metadata y junto a cada raw/published output. No vale `main`, `latest` ni sólo un branch/tag. No usar el SHA de esta rama como sustituto si el run no se ejecuta allí.
- Para los artefactos históricos OLD, registrar su commit de ejecución de procedencia exacto: para M2 se documenta `MAIN_SHA=607ff21` únicamente como prefijo histórico (`OLD_M2_EXECUTION_SHA_FULL_VERIFIED=NO`, `OLD_M2_EXECUTION_SHA_PREFIX=607ff21`), prohibiendo su inferencia o extrapolación a un SHA completo de 40 hex; para M3 se documenta el baseline histórico citado en checkpoint (`1a52c3ea`) sin inventar un SHA completo. El comparador y sidecars registrarán explícitamente `old_execution_commit` con tipo `historical_7_hex_prefix` y `full_40_hex_verified: false`.
- Registrar `OS`, `Python`, `NumPy`, `Pillow`, implementación/backend FFT (`numpy.fft` y configuración/versiones pertinentes), repo SHA, hash/bytes de cada manifiesto usado y hashes de assets verificados.
- Por cada artefacto guardar: raw execution artifact, SHA-256 raw, byte count raw, EOL del raw; published Git artifact, SHA-256 del blob Git, byte count published, y nota exacta de normalización EOL. Validar igualdad semántica/JSON tras la normalización. No introducir anotaciones post-run en el JSON de resultados; cualquier metadato de protocolo vive fuera o se declara como transformación con hash y diff.
- Mantener los OLD artifacts inmutables, en particular `docs/validation/native-parallax-revalidation-20260928/**`, el histórico M2 y `data/exp-m4-results.json` / `data/exp-m4-calibration.json`. No editar tablas, JSON ni manifiestos históricos.
- M2 y M3 runners no proporcionan por sí solos toda la provenance de ejecución requerida: acompañar sus outputs con sidecar de comparación/README que registre los campos anteriores y preserve RAW vs. published.

Environment histórico se transcribe como referencia, no como environment NEW: M2 documenta Linux 6.1/Python 3.11.2/NumPy 2.4.6/Pillow 12.3.0; M3 checkpoint/doc registra Windows/Python 3.11.9/NumPy 2.4.6/Pillow 11.3.0. Diferencias de environment se reportan, no se ocultan ni se presentan como efecto matemático.

## 10. Namespace y layout planeado (no creado en REVAL-0)

Namespace planeado al congelar este protocolo:

```text
docs/validation/native-parallax-math-revalidation-20261005/
```

Si REVAL-1 ocurre en otra fecha, utilizar la fecha UTC real de ejecución en el nombre del namespace y registrarla antes de escribir outputs. Layout mínimo:

```text
README.md
m2/
    rows.json
    characs.json
m3/
    exp_m3_results.json
comparison/
    m2-old-vs-new.json
    m3-old-vs-new.json
    impact-report.md
```

El árbol Git guarda artefactos publicados. Los RAW se preservan fuera del árbol de assets/repositorio o en el almacenamiento de research establecido, con su hash/bytes/EOL en `README.md` y comparison metadata. REVAL-0 no crea estos directorios ni outputs.

## 11. Secuencia de ejecución futura y hard stops

Los comandos siguientes son una especificación de REVAL-1, **no se ejecutan en REVAL-0**. `M2_MANIFEST_VERIFIED` debe señalar el manifiesto local histórico recuperado y autenticado (`C:\SkyClawResearch\NativeParallax\EXP-M3\cohort_b\exp-m2-authored-manifest-local.json`); no tiene default automático al manifiesto canónico del repo.

1. Requerir antes de tocar corpus: freeze/merge del protocolo tras revisión Tech Lead + revisión adversarial independiente Arena AI + CI green; registrar adjudicación de findings. Los reviewers señalan riesgos, pero sus comentarios no cambian parámetros por sí mismos.
2. Artefactos OLD y manifests autenticados: RECOVERY-0 completó la recuperación y autenticación de `rows.json` (174 filas), `characs.json` (34 assets), el manifiesto local M2 y el RAW M3 histórico. Los artefactos OLD quedan preservados de forma externa e inmutable.
3. Corpora montados y verificados: RECOVERY-0 completó la verificación criptográfica SHA-256 de los 68 archivos de M2 (Cohort B en `C:\SkyClawResearch\NativeParallax\EXP-M3\cohort_b_mirrors`) y de los 62 archivos de M3 (Cohort A en `C:\SkyClawResearch\NativeParallax\EXP-M3\originals`).
4. Pre-flight obligatorio de resolución de rutas (`PATH_PREFLIGHT_REQUIRED=YES`):
   Antes de invocar cualquier runner científico en REVAL-1, se debe realizar un pre-flight obligatorio que resuelva y verifique:
   - M2 manifest path
   - M2 corpus root (`C:\SkyClawResearch\NativeParallax\EXP-M3\cohort_b_mirrors`)
   - M3 manifest path
   - M3 corpus root (`C:\SkyClawResearch\NativeParallax\EXP-M3\originals`)
   Para cada entidad registrar:
   ```text
   absolute resolved path
   exists
   file/directory type
   ```
   Para manifiestos con rutas relativas/internas hacia assets: resolverlos bajo las mismas reglas deterministas del runner y verificar la existencia física de cada asset antes de iniciar la corrida.
   Si cualquier ruta esperada no existe o falla la resolución:
   ```text
   STOP
   REVALIDATION_EXECUTION_ALLOWED=NO
   BLOCKER=CORPUS_PATH_PREFLIGHT_FAILED
   ```
   Prohibición absoluta: NO crear rutas, NO recrear junctions y NO descargar assets durante o para eludir el pre-flight.
5. Autorización técnica de ejecución: `REVALIDATION_EXECUTION_ALLOWED=YES` condicionado a la aprobación de todos los gates y superación del pre-flight de rutas, una vez congelado el protocolo y obtenido el commit exacto `RUN_CODE_SHA`. Esto NO significa ejecutar ahora en esta fase documental.
6. Confirmar en el commit `RUN_CODE_SHA` los valores congelados de la sección 4, resolución 512 y rutas del runner; capturar environment antes de ejecutar. Confirmar output nuevo y único, sin sobrescribir OLD.
7. Sólo tras todos los gates, comandos conceptuales:

```bash
python -m sky_claw.local.native_parallax.research.run_exp_m2 \
  --manifest "$M2_MANIFEST_VERIFIED" \
  --resolution 512 \
  --out "$RAW_RUN_ROOT/m2"

python -m sky_claw.local.native_parallax.research.run_exp_m3 \
  --m3-manifest docs/design/research/native-parallax/data/exp-m3-clean-authored-manifest.json \
  --m2-manifest "$M2_MANIFEST_VERIFIED" \
  --resolution 512 \
  --out "$RAW_RUN_ROOT/m3"
```

8. Si M2 falla identidad/provenance o pre-flight de rutas, **no ejecutar M2 ni M3**: Cohort B M3 depende del corpus/manifest M2 y la pregunta conjunta queda bloqueada. No ejecutar sólo una parte para presentarla como comparación completa.
9. Calcular/preservar hashes raw y published, generar comparación estructurada y tie diagnostics; investigar toda variación no esperada antes de cualquier interpretación.

## 12. Cambios esperados y no esperados

**Esperados/candidatos que se verifican por camino causal:**

```text
curl proxy values may change
projection/oracle diagnostics may change
M3 Cohort B membership at frozen oracle thresholds may change
Spearman values with ties may change
proxy rankings / proxy winner / orientation may change
bootstrap degeneracy accounting may appear
fallback sigma winner may disappear if not evaluable
NaN/Inf textual tokens become null (serialization only)
```

**No esperados; requieren `INVESTIGATE_BEFORE_INTERPRETATION`:**

```text
asset membership/hash/provenance changes
manifest semantics or split changes
threshold, K_TRANSFER, K_SWEEP, seed, bootstrap n, catastrophic rule changes
RAW geometry deltas not consuming an actually corrected primitive
policy changes without a traceable sigma/proxy change
M3 decision change without a causal path through recorded metrics/rules
new 1024 primary result or altered filters/subsets
unexplained EOL/JSON edits or OLD artifact mutation
```

Cualquier cambio no esperado detiene la interpretación. No se explica como “ruido” ni se corrige retuneando.

## 13. Review, aprobación y estados del freeze

- `PRIMARY_EXTERNAL_ADVISORY_REVIEW=ARENA_AI`.
- Antes del corpus real: revisión adversarial independiente de Arena AI y revisión de Tech Lead; revisar post-hoc degrees of freedom, métricas ausentes, overwrite de artifacts, deriva de corpus, atribución causal, retuneo oculto y reescritura del protocolo histórico.
- Qodo no es gate requerido/disponible en esta fase: `QODO_REQUIRED=NO`.
- Registrar findings y adjudicación en el PR. No modificar el protocolo automáticamente por una opinión; cualquier cambio aceptado se revisa y congela antes del run, nunca después de ver NEW.
- La PR de este documento se abre Draft, no se mergea desde este turno. El run real requiere review aprobada, CI green y protocolo mergeado/freezeado. Sólo entonces puede comenzar REVAL-1.
- #675 queda fuera de alcance, sin cambios: abierto, Draft, `M6_IMPLEMENTATION_BLOCKED=YES`.

### 13.1 Adjudicación y codificación formal de findings de Arena AI

Auditoría adversarial externa completada por Arena AI:
```text
ARENA_AI_REVIEW_STATUS=PASS_WITH_FINDINGS
ARENA_FINDINGS_ENCODED=YES
```

Adjudicación exhaustiva de findings incorporados en este documento:
1. **Finding-01 (Path Preflight — Resolución determinista de rutas antes de ejecución):**
   - *Estado:* ACEPTADO / codificado en el protocolo (§11).
   - *Detalle:* Contrato `PATH_PREFLIGHT_REQUIRED=YES`. Previo a invocar cualquier runner científico, resolver y verificar la existencia física de rutas para M2 manifest, M2 corpus root, M3 manifest, M3 corpus root y assets internos. Cualquier fallo genera `CORPUS_PATH_PREFLIGHT_FAILED` y detiene la ejecución inmediatamente. Prohibición estricta de crear rutas intermedias, recrear junctions o descargar sustitutos.
2. **Finding-02 (SHA histórico M2 — Prefijo `607ff21` sin atestación 40-hex completa):**
   - *Estado:* ACEPTADO / codificado en el protocolo (§2.2, §7 y §9).
   - *Detalle:* Contrato `OLD_M2_EXECUTION_SHA_FULL_VERIFIED=NO` y `OLD_M2_EXECUTION_SHA_PREFIX=607ff21`. Prohibida la extrapolación o inferencia a 40 hex. El sidecar y comparador NEW registran el objeto `{ "old_execution_commit": { "value": "607ff21", "kind": "historical_7_hex_prefix", "full_40_hex_verified": false } }`. La ejecución NEW sí exige `RUN_CODE_SHA=<40 hex exacto>`.
3. **Finding-03 (Toolchain Epsilon — Clasificación estricta de divergencias por precisión floating):**
   - *Estado:* ACEPTADO / codificado en el protocolo (§7).
   - *Detalle:* Clasificación `POTENTIAL_TOOLCHAIN_FLOATING_NOISE`. No relaja la comparación ni autoriza tolerancias numéricas para declarar equivalencia científica, ni modifica umbrales ni redondea deltas. Requiere delta a nivel de precisión de máquina (~1e-16 / LSB IEEE-754), cero cambio de estado de decisión o categórico, y ausencia de explicaciones causales MATH-A/MATH-B, registrando exhaustivamente metadatos de entorno (OS, Python, NumPy, Pillow, FFT backend).

## 14. Estado final REVAL-0

```text
REVAL_PROTOCOL_COMPLETE=YES
DOC_ONLY=YES
REAL_CORPUS_TOUCHED=NO
M2_RERUN_EXECUTED=NO
M3_RERUN_EXECUTED=NO
M6_REAL_RUN_EXECUTED=NO

M2_HISTORICAL_REFERENCE_LOCATED=YES
M3_HISTORICAL_REFERENCE_LOCATED=YES
M2_MANIFEST_LOCATED=YES
M2_LOCAL_MANIFEST_LOCATED=YES
M2_LOCAL_MANIFEST_SHA256=40ff24bd3342c60d6d23d700975ae5d4f37adffc96bb900aaaf0ecea58d5a34a
M2_CORPUS_IDENTITY_VERIFIED=YES
M2_FILES_SHA256_MATCH=68/68
M2_HISTORICAL_ROWS_LOCATED=YES
M2_HISTORICAL_CHARACS_LOCATED=YES
M2_ROWS_SHA256=c2d8328e62046245d4c395ee0ecaad3696b8fb48f6578c9582079182ad770a08
M2_CHARACS_SHA256=9afb60a453d2783572145c3d270145b2dc668941bbbdf96a5ac25cf7cef7a683
M2_OLD_ARTIFACTS_VERIFIED=YES

M3_MANIFEST_SHA256=b0f5a4c6604989269647973b6a6e6b899e859b436e7b42bede7e3297d10e6d6f
M3_CORPUS_IDENTITY_VERIFIED=YES
M3_FILES_SHA256_MATCH=62/62
M3_HISTORICAL_RAW_LOCATED=YES
M3_HISTORICAL_RAW_SHA256=047769fdad05e9af7960feb2c414470af7493d847e0901bac51d7c4a84bc5e19
M3_PUBLISHED_SHA256=02f6d1ac5a679081359b43a173f8e849fa9819e0d1c4e60830266a337b2335da
M3_RAW_PUBLISHED_SEMANTIC_EQUALITY=YES
OLD_ARTIFACTS_IMMUTABLE=YES

MATH_A_METRICS_MAPPED=YES
MATH_B_METRICS_MAPPED=YES
REPRO_A_CLASSIFIED_SERIALIZATION_ONLY=YES
SPEARMAN_TIE_DIAGNOSTICS_PREDEFINED=YES
DECISION_EQUIVALENCE_PREDEFINED=YES
TOOLCHAIN_FLOATING_CLASSIFICATION_PREDEFINED=YES
NO_RETUNING=YES

PATH_PREFLIGHT_REQUIRED=YES
OLD_M2_EXECUTION_SHA_FULL_VERIFIED=NO
OLD_M2_EXECUTION_SHA_PREFIX=607ff21

ARENA_AI_REVIEW_REQUIRED=YES
ARENA_REVIEW_STATUS=PASS_WITH_FINDINGS
ARENA_FINDINGS_ENCODED=YES
QODO_REQUIRED=NO
M6_IMPLEMENTATION_BLOCKED=YES

NEW_ARTIFACT_NAMESPACE=docs/validation/native-parallax-math-revalidation-20261005/
REVALIDATION_EXECUTION_ALLOWED=YES
RECOVERY_BLOCKER=NONE
READY_FOR_TECH_LEAD_REVAL0_REVIEW=YES
```

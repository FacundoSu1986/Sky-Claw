# ARQUITECTO A — Generador nativo de altura 100% determinista (sin IA)

> Postura: el problema "normal map → height map → DDS válido para Skyrim" es un problema
> **de procesamiento de señales con contratos de formato conocidos**. Toda la incertidumbre
> real está en (1) la baja frecuencia y el signo de la altura, (2) la clasificación de
> candidatas, y (3) la calidad percibida. Ninguna de las tres se resuelve mejor con IA sin
> pagar en determinismo, licencias y auditabilidad. Este documento propone la versión
> clásica más fuerte posible y define los experimentos que podrían refutarla.

---

## A. Sources researched

Fuentes compartidas: `10_ecosystem_facts.md` (tablas 1.1–1.8, con enlace por fila).
Adicionalmente consultadas para esta arquitectura:

| Claim | Fuente | Fecha de consulta | Confianza |
|---|---|---|---|
| FC = proyección sobre base Fourier; válido si la superficie es periódica; error 2π en implementaciones públicas | [Quéau, Durou, Aujol — Normal Integration: A Survey (arXiv:1709.05940)](https://arxiv.org/abs/1709.05940) | 2026-09-21 | VERIFIED (leído extracto PDF) |
| Poisson DCT no iterativo (Simchony-Chellappa-Shao) | ídem §3.2 | 2026-09-21 | VERIFIED |
| Todas las reconstrucciones Poisson de un mismo campo de gradientes difieren en términos armónicos; DC no observable | [Agrawal, Raskar, Chellappa ECCV 2006](https://link.springer.com/chapter/10.1007/11957959_72) (vía survey) | 2026-09-21 | SUPPORTED_BY_UPSTREAM |
| Bordes periódicos por Poisson con lados opuestos iguales | [Pérez et al. 2003, Poisson Image Editing](https://www.cs.jhu.edu/~misha/Spring07/perez03.pdf) | 2026-09-21 | SUPPORTED_BY_UPSTREAM |
| FC difuso para ruido/gaps; p=-n1/n3, q=-n2/n3 | [Fuzzy Frankot–Chellappa, Algorithms 2025](https://www.mdpi.com/1999-4893/18/8/488) | 2026-09-21 | SUPPORTED_BY_UPSTREAM |
| Licencias: texconv MIT; Compressonator MIT; nvtt MIT; bcdec Unlicense; Pillow escribe BC1-BC5 (no BC4/BC7) | `10_ecosystem_facts.md` §1.6 (arXiv de fuentes individuales) | 2026-09-21 | VERIFIED |
| Práctica pública normal→altura con Poisson+FFT en la comunidad Skyrim | [Nexus article 7202](https://www.nexusmods.com/skyrimspecialedition/articles/7202) | 2026-09-21 | SUPPORTED_BY_UPSTREAM |
| Decoders BCn comparados por calidad/velocidad; squish BC4/5 decode roto | [Aras-P, Comparing BCn decoders](https://aras-p.info/blog/2022/06/23/Comparing-BCn-texture-decoders/) | 2026-09-21 | VERIFIED |

## B. Current Skyrim parallax model (visión desde A)

Contrato completo en `10_ecosystem_facts.md` §1.1–1.3. Decisiones que A deriva de él:

1. **Objetivo inicial = `*_p.dds` legacy de objeto** (BC4, canal rojo, slot de parallax):
   contrato unicanal más simple, idéntico para ENB y CS, y PGPatcher hace el mesh.
   [VERIFICADO]
2. **Segundo objetivo = Complex Material** (env mask `_m`/`_em` con R=envmask, G=gloss,
   B=metal, **A=altura**; BC3/BC7 con alfa): requiere además saber escribir gloss/metal
   sensatos; se delega en una fase posterior.
3. **Terrain = NO para el MVP**: el alfa del diffuse tiene semántica dual (altura neutral 127
   + specular) y un mal alfa rompe texturas existentes; riesgo/valor desfavorable de entrada.
   [INFERENCIA]
4. **TruePBR =NO para el MVP**: requiere JSON + rutas `pbr/` + escala por material; es un
   exporter futuro del mismo HeightField. [INFERENCIA]
5. La prioridad 2026 es CS (Extended Materials en core, activo por defecto para objetos), con
   validez simultánea en ENB porque el contrato `_p` es común. [VERIFICADO + INFERENCIA]

## C. Problem decomposition

```
E0 discovery      → vista efectiva de texturas (Sky-Claw ya tiene MO2)
E1 inspección     → formato, tamaño, canales, alfa, tileabilidad, sRGB flag, mip chain
E2 clasificación  → ¿candidata? ¿ya tiene altura? ¿semántica de alfa conocida?
E3 decodificación → normal → (nx,ny,nz) float, convención DirectX, manejo BC5/BC7/BC1
E4 gradientes     → p=∂z/∂x, q=∂z/∂y desde la normal (+ escena de confianza por pixel)
E5 integración    → campo posiblemente no integrable → H(x,y) por Poisson periódico (FFT)
E6 restauración LF→ DC + baja frecuencia: alineación de rango, resta de mediana, pesos
E7 normalización  → percentiles robustos → [0,1] float32; decisión de signo documentada
E8 validación     → métricas vs la propia normal y contra GT sintético calibrado
E9 export         → mips → BC4 (_p) | BC7 (CM) → output administrado + manifest
```

Separación clave que A defiende: **E3–E7 son puros** (sin IO, sin Skyrim, deterministas,
unit-testeables con numpy); E0–E2 y E9 son integración. Esta separación es la que permite
el benchmark sintético (§O, EXP-001/002).

## D. Proposed architecture

```
EffectiveAssetProvider (Sky-Claw)
        │  logical path + bytes + provenance
        ▼
TextureInspector ──► CandidateClassifier (reglas + señales, sin IA)
        │ candidatas con reason codes
        ▼
NormalDecoder ──► GradientField ──► PeriodicPoissonIntegrator ──► HeightNormalizer
        │                                              │
        │                                    LowFrequencyRestorer (albedo-aware, opcional)
        ▼                                              ▼
   QualityValidator (métricas deterministas) ──► HeightField (inmutable DTO)
                                                       │
                                             DdsExporter (texconv externo / Pillow)
                                                       ▼
                                        ManagedOutput ("Sky-Claw - Native Parallax Output")
                                                       ▼
                                                  Manifest + cache
```

Principios:

- **Backend único real** (`NORMAL_INTEGRATION_PERIODIC`); la abstracción
  `HeightGenerationBackend` se pospone (§32 del brief) hasta que exista un segundo backend
  que sobreviva a los mismos gates. Una interfaz con una implementación es código muerto.
  [INFERENCIA]
- Cada textura produce un **result DTO** con estado y reason codes; el runner no tiene
  framework de orquestación propio más allá de un worker pool con bounded concurrency.
- Todo output vive en un mod de salida propio (PRESERVE_EXISTING por defecto) con manifest
  por archivo (§22 del brief). Nada escribe en mods fuente ni pisa `_p` existentes.

## E. Algorithms (matemática revisable)

### E.1 Decodificación de normal

- DDS → plane RGB(A) float32. Si el formato es BC5: `nx=2R-1, ny=2G-1, nz=sqrt(max(0,1-nx²-ny²))`.
  Si es RGB(A) de 8-bit: `n=(2c/255-1)`, renormalizar `n/||n||` (los normales "2-channel con
  Z implícito" en 8-bit RGB son raros pero existen; detectar por azimut de B).
- Convención DirectX (verificada, `10_ecosystem_facts.md` §1.4): el canal G de Skyrim vale
  `ny_directx = -ny_gl`. Definimos el gradiente en el espacio de la textura (x→derecha,
  y→**abajo**, coherente con el orden de filas del DDS):
  `p = ∂z/∂x = -sx·nx/nz`, `q = ∂z/∂y = -sy·ny/nz` con `sy=+1` si el G del archivo es
  DirectX-Y-abajo, `sy=-1` si es OpenGL. **El signo por-defecto y su verificación son una
  decisión de diseño con test** (EXP-005), no una suposición.
- Confianza por píxel: `w = nz` (píxeles de pared pierden información de altura) y
  `w_edge = 1 - var_local(n)` para penalizar discontinuidades donde la integración global
  mancha.

### E.2 Integración periódica (default para texturas tiled)

Proyección de Frankot-Chellappa **con base Fourier periódica**, que es exactamente la
solución del Poisson periódico:

```
Ĥ(u,v) = (-i·u·P̂(u,v) - i·v·Q̂(u,v)) / (u² + v²)    para (u,v) ≠ (0,0)
H = IFFT(Ĥ);   Ĥ(0,0) := 0  (DC no observable)
```

con `P̂,Q̂ = FFT(p,q)` y frecuencias de cuadrícula `u=2πk/W`, `v=2πh/Hh`. Coste O(N log N),
memoria O(N), **sin costuras por construcción** (los lados opuestos son la misma muestra en
la base periódica). La ambigüedad DC/lineal queda implícita: la solución es de media cero
[Agrawal 2006; survey §3.3].

### E.3 No-integrabilidad y robustez

El campo (p,q) de normales reales no es integrable (cuantización 8-bit, BC, AO horneada en
el canal B, autoría manual). Opciones, en orden de adopción:

1. **Poisson periódico** = proyección L2 integrable. Robusto a ruido blanco de baja energía;
   difumina discontinuidades (known issue del survey).
2. **Poisson ponderado por confianza** (Weighted Least Squares): minimizar
   `Σ w_p (z_x - p)² + w_q (z_y - q)² + λ Σ w_z ((z_x)_y - (z_y)_x)²`
   resuelto en la práctica como Poisson con término fuente re-ponderado en 2-3 iteraciones
   de re-weighting (IRLS). Mantiene FFT si los pesos son suaves; si no, DCT por bloques con
   overlap. [INFERENCIA sobre la familia robust del survey §4]
3. **Multi-escala coarse-to-fine**: pirámide Gausiana de (p,q) (re-proyectada a normal y
   re-integrada por nivel), corrigiendo el drift de baja frecuencia por niveles; controla el
   "inflado global" que la integración pura produce con gradientes sesgados.

### E.4 Restauración de baja frecuencia (la parte honesta)

Matemáticamente **ningún** método puede recuperar DC ni los modos armónicos del campo de
gradiente [Agrawal 2006]. La respuesta determinista de A:

- El output se **centra por percentiles** (p2..p98) → [0,1]; la escala absoluta no es
  información, la fija el usuario por material-family (constante por defecto, editable).
- Restauración opcional de LF desde el albedo: las hendiduras acumulan oclusión ⇒
  `LF ≈ -blur(albedo_luminancia_normalizada)` fusionada en banda baja con ganancia α∈[0,1]
  calibrada en el benchmark sintético. **Se marca como heurística explícita, OFF por
  defecto, con métrica propia** (EXP-006). A no promete que funcione en general: albedo
  oscuro ≠ hueco (ej. carbón).
- **Inversión (quién sobresale)**: A lo resuelve con evidencia local de la escena:
  (a) correlación de H contra la "dirt/AO" del albedo; (b) para ladrillo/piedra: detectar el
  canal de juntas por saturación/borde y exigir juntas ≤ altura media; (c) consistencia con
  la skew de la distribución (materiales de base plana dominan → pico de histograma = base).
  Cuando (a)-(c) no acuerdan → **REVIEW_RECOMMENDED**, no AUTO_ACCEPT. Es la debilidad
  reconocida del enfoque clásico y por eso es gate, no heurística silenciosa. [HIPÓTESIS]

### E.5 Normalización y mips

- Normalización: percentil robusto + clip suave (soft-knee al 1%); prohibition de
  auto-contrast agresivo (amplifica ruido BC).
- Mips: para height fields **el promedio de área es correcto** (la altura es un campo
  escalar continuo, no un dato de máscara); lo que NO es correcto es dejar que texconv
  filtre con kernels de chroma o sRGB. Generamos mips en float32 con área-average y
  optionally "min-preserve" para las 2 últimas mips (evita que el relieve desaparezca a
  distancia). [HIPÓTESIS → EXP-010]

## F. Candidate classification

Señales y orden de decisión (todas derivadas del ecosistema, no de terceros):

| # | Señal | Regla | Reason code si falla |
|---|---|---|---|
| 1 | Existe `_p`/`_m` con alfa-no-trivial en el mismo prefijo | skip | `already_has_height` |
| 2 | Vecino `_n.dds` (mismo basename) decodificable | requerido para ruta normal | `no_normal_map` |
| 3 | Contraste del campo de gradientes (energía HF) sobre umbral calibrado | skip si plano | `insufficient_signal` |
| 4 | Dimensiones ≥64 y potencia-de-2 (mip-able) | skip | `unsupported_dimensions` |
| 5 | Semántica de alfa de la normal conocida (vacío=OK; specular/AO=NO para CM) | skip | `alpha_semantics_unknown` |
| 6 | Prefijo/ruta de UI/faces (`textures\interface`, `actors\character\faces`…) | skip | `unsupported_material` |
| 7 | Ya es PBR (json `pbr/` o `_rmaos`) | skip | `already_PBR` |
| 8 | Usuario/config | override | `user_excluded` |

Comparativa honesta de enfoques: filename-only es insuficiente y frágil (falso positivo:
`_n` de una cara); inspection-only pierde el uso real; **el graph de NIF no lo construye A**
— se delega a PGPatcher, cuyo matching es por prefijo de textura y cuyas exclusiones de
mesh ya están verificadas (`10_ecosystem_facts.md` §1.3). En A la IA queda fuera por diseño
(§L).

## G. Height reconstruction (resumen operativo)

1. `n` decodificada → `p,q` (E.1) → 2. integrabilidad residual `ρ = mean|∂p/∂y - ∂q/∂x|` (diagnóstico) →
3. FC periódico (E.2) → 4. opcional IRLS 2-3 iter + multi-escala (E.3) → 5. LF opcional (E.4) →
6. verificación de signo: recomputar normal desde H (`nx=-H_x/s, ny=+H_y/s` DirectX) y medir
error angular medio vs original; si >umbral → REJECT (el gradiente se extrajo mal). Este
paso **auto-valida la convención** sin fe. → 7. normalización E.5.

## H. DDS / export strategy

- **Ruta A (default)**: `texconv` (DirectXTex, MIT) como **herramienta externa** invocada:
  `-f BC4_UNORM -m <n> -srgb off` sobre un TGA/PNG/TIFF intermedio. Sky-Claw ya tiene
  tool registry + version detection; texconv encaja como `DYNAMIC_EXTERNAL_TOOL`
  (licencia MIT, redistribuible, headless, Windows). [VERIFICADO §1.6]
- **Ruta B (fallback en procesos puros)**: Pillow 11.2.1+ escribe **BC3/BC5** (no BC4/BC7);
  útil para tests y para CM provisional; BC4 final siempre vía texconv/Compressonator.
  [VERIFICADO §1.6]
- Decodificación DDS de entrada: Pillow (lectura) + `bcdec`-style verificación con
  `texture2ddecoder` [NO VERIFICADO versión]; nunca squish para BC4/5 (decode roto
  [VERIFICADO §1.6]).
- Mips: se generan en float32 propios y se pasan pre-hechos (`--mipmap-mode` custom) o se
  dejan generar con filtro LINEAR (área) — nunca POINT; sRGB flag OFF para todo canal de
  altura. Determinismo: EXP-010 mide estabilidad entre versiones de la herramienta; el
  manifest registra `encoder_id+version`.

## I. Validation / quality gates (métricas deterministas)

Todas calculables sin GPU, en float32:

| Métrica | Definición | Gate propuesto (calibrar en EXP-001/002) |
|---|---|---|
| `normal_agreement` | error angular medio (°) entre normal original y normal derivada de H tras alinear con (a,b) óptimos por mínimos cuadrados y elegir mejor signo | pass si < 15° mediana; REJECT si > 25° |
| `integrability_residual` | `mean|p_y - q_x|` normalizado | advertencia si > τ_int; no rechaza solo |
| `seam_score` | `mean|H[0,:]-H[-1,:]| + |H[:,0]-H[:,-1]|` tras integración periódica vs borde teórico 0 | pass si < τ_seam (EXP-002) |
| `dynamic_range` | p98-p2 de H | advertencia si < 0.08 (plano) |
| `clip_ratio` | % píxeles en {0,1} | advertencia si > 2% |
| `banding_index` | entropía local del histograma / escalones | advertencia (post-BC4) |
| `flatness_hf` | energía HF de H vs de la normal | coherencia espectral |
| `nan_inf` | conteo | REJECT si > 0 |
| `tileability_continuity` | coseno entre gradientes de borde opuestos | pass si > 0.9 |
| `mip_consistency` | normal_agreement recomputada en mip1/mip2 | advertencia |

Los thresholds **no se inventan**: se fijan como percentiles del rendimiento del pipeline
sobre el benchmark sintético (§O) y se congelan con versión de algoritmo. [INFERENCIA
metodológica]

## J. Performance

- Coste por textura 2K: decode ~40-80 ms; FFT 2048² float64 compleja ~200-400 ms CPU
  (pocket: numpy/scipy); mips+encode ~100 ms; total < 1 s/núcleo. 4K ~4-5x. [INFERENCIA
  de órdenes de magnitud; se mide en EXP-001]
- Presupuesto 5.000 candidatas 2K: ~1,5-2 h en 8 workers (paralelismo por proceso, bounded
  concurrency 2×cores); 50.000 texturas 4K: ~1-2 días → cache de contenido obligatoria
  (§49 del brief): `SHA256(inputs+algo+params+encoder)` → skip si existe.
- RAM: streaming por textura (nunca>2 texturas 4K en memoria por worker); disco temporal
  ~3× textura; VRAM 0 (CPU-only).
- Hash cache + skip de no-candidatas baratas primero (inspección de headers sin decode
  completo).

## K. Sky-Claw integration

- Capas nuevas bajo `sky_claw/local/` (módulo propuesto `native_parallax/`), sin acoplarse a
  DynDOLOD/Runtime Vault lanes: consume `EffectiveAssetProvider` (interfaz nueva, adapter a
  MO2 VFS ya existente en Sky-Claw) y produce `ManagedOutput` con output-ownership ya
  estandarizado en el repo.
- PGPatcher permanece **aguas abajo, invocado como herramienta externa** (contratos ya
  verificados en P0: output mod deshabilitado durante el run, `ParallaxGen_Diff.json` para
  DynDOLOD, `PG_<N>.esp` antes de TexGen/DynDOLOD). Nuestro generador NUNCA toca NIF.
- Journal/HITL: texturas en REVIEW aparecen en la cola HITL existente con preview + reason.
- Transactional state: el run es idempotente (misma cache key ⇒ skip); el output mod se
  publica como transacción atómica (staging dir → swap).

## L. AI role (dónde A NO usaría IA)

- **Core de reconstrucción: jamás.** La altura desde normal tiene solución exacta
  (proyección L2); un modelo que la "estime" añade varianza sin información nueva en HF.
- **Clasificación candidata: no en MVP.** Las señales 1-8 son baratas y auditables; un
  clasificador semántico sería el *primer* candidato a IA si A perdiera el debate, pero su
  valor solo existe donde las reglas son ciegas (¿"cara" sin ruta? ¿metal decorativo?) —
  dominio que la convención de rutas y el matching de PGPatcher ya cubren en gran parte.
- **Inversión/LF: NO con IA generativa**; con heurísticas acotadas + gate humano.
- Donde A **sí** aceptaría IA (como herramienta de autor, no de pipeline): generación del
  benchmark sintético (variantes de materiales) y análisis exploratorio de fallos. Ninguna
  de las dos toca outputs del usuario.

## M. Clean-room compliance

- Ningún input de ParallaxR: ni código, ni strings, ni exclusiones, ni presets. La
  clasificación se deriva solo del §1.3 (PGPatcher, GPL-3, público) + inspección propia.
- La existencia de herramientas de la categoría (ParallaxR, Skyking, generadores web) se usa
  únicamente como prueba de demanda; sus métodos no informan decisiones.
- El doc P0 del repo se usa para contratos de invocación de PGPatcher (GPL-3, público), no
  para algoritmos.

## N. License / dependency audit

| Dependencia | Licencia | Clasificación |
|---|---|---|
| numpy, scipy | BSD-3 | SAFE_TO_DEPEND |
| opencv-python | Apache-2.0 | SAFE_TO_DEPEND (solo si se justifica; numpy+scipy bastan) |
| Pillow | MIT-CMU (HPND) | SAFE_TO_DEPEND |
| texconv / DirectXTex | MIT | DYNAMIC_EXTERNAL_TOOL (binario externo, no linkeado) |
| Compressonator | MIT (+bc7e Apache-2.0) | OPTIONAL (alternativa cross-platform) |
| bcdec | Unlicense | NEEDS_REVIEW (dominio público reconocible; en algunos países europeos el PD-like requiere revisión; alternativa: Pillow read) |
| texture2ddecoder | MIT (verificar) | OPTIONAL |
| Ningún GPL importado | — | política: GPL solo como binario externo separado (PGPatcher ya se invoca así en Sky-Claw) |

## O. Experiments EXP-001..012 (propuesta de A)

Formato: hipótesis / dataset / procedimiento / métrica / umbral propuesto / lectura del fallo.

- **EXP-001 Sintético normal→altura.** H. "La integración periódica recupera ≥90% de la
  varianza de H tras alineación (a,b) y mejor signo". D: 100 materiales MatSynth CC0 →
  H_gt → normal sintética (sin ruido y con ruido σ=1/255 y σ=4/255). P: pipeline E.1-E.7.
  M: RMSE(H_aligned), SSIM, error angular. T: RMSE<0.05 limpio, <0.09 ruido alto. Fallo ⇒
  revisar convención de signos o usar multi-escala.
- **EXP-002 Tileabilidad.** H. "seam_score≈0 con FFT periódico y ≫0 con DCT/Neumann". D:
  ídem EXP-001 + mosaicos 2×2. M: seam_score + costuras visuales en mosaico. T: seam<0.01
  FFT. Fallo ⇒ bug de frecuencias/escala de la FFT.
- **EXP-003 Sensibilidad BC.** H. "BC5 en normales degrada agreement <5° vs sin comprimir;
  BC1 (normales en BC1) lo degrada >15°". D: EXP-001 + re-codificar normal a BC5/BC1/BC7
  vía texconv. M: normal_agreement por formato. T: como H. Fallo ⇒ exigir decode cuidadoso
  (media de bloque) o rechazar BC1.
- **EXP-004 Rechazo de planas/ruidosas.** H. "energía HF + integrability_residual separan
  candidatas de no-candidatas sin falsos positivos >5%". D: 500 texturas pl/UI/flat vs 500
  materiales. M: ROC de la regla. Fallo ⇒ añadir señal de tiling/uso.
- **EXP-005 Escala/inversión.** H. "el signo óptimo es seleccionable con agreement angular;
  la selección semántica (quién sobresale) acierta <70% en piedra/ladrillo". D: sintético +
  50 materiales reales etiquetados a mano. M: accuracy. T: flip mecánico ≥95%; semántico
  60-70% esperado. Fallo ⇒ el gate REVIEW es permanente para casos ambiguos.
- **EXP-006 LF desde albedo.** H. "fusionar LF de luminancia mejora RMSE LF <10% solo en
  materiales con AO legible". D: MatSynth (AO disponible). M: RMSE por bandas. Fallo ⇒
  feature OFF por defecto para siempre.
- **EXP-010 DDS/mips.** H. "BC4+área-mips conserva agreement; mips POINT o sRGB ON lo
  destruyen". M: agreement por mip. T: degradación <3° por mip.
- EXP-007/008/009 (IA), EXP-011/012 (previews): definidos para comparar contra A en
  `40_critica_cruzada.md` y `50_sintesis_c.md`; A los formula como **tests de B**, no como
  parte de su MVP.

## P. MVP-SAFE

- Alcance: E3→E7 puros + validador + escritura **PNG/TIFF** (sin DDS aún) + EXP-001/002
  verdes sobre 20 materiales CC0.
- Deps: numpy, scipy, Pillow, pytest. Files: `normal_decoder.py`, `poisson_periodic.py`,
  `metrics.py`, `benchmark_synth.py`.
- Criterio de aceptación: RMSE(H)<0.05 (limpio) y seam<0.01 en 20/20.
- Esfuerzo: 2-4 días. Incógnitas: convención de signo (EXP-005 mecánico).
- Falsable si: el error angular tras integración no baja de 25° en limpio (bug de concepto).

## Q. MVP-BALANCED

- MVP-SAFE + texconv wrapper (BC4) + classification por reglas + quality gates + ManagedOutput
  + manifest + hash cache. Target: 100 texturas de un mod CC0 (AmbientCG-based) → `_p.dds`
  BC4 → rig offline → 20 en juego con PGPatcher en un perfil chico.
- Criterios: 0 NaN, agreement mediano <15°, seam<τ, output en mod propio, HITL REVIEW para
  signo ambiguo. Esfuerzo: 2-3 semanas.
- Falsable si: en el rig in-game >20% de las texturas se perciben peor que baseline
  (EXP-012).

## R. MVP-RESEARCH

- MVP-BALANCED + LF restoration albedo-aware + IRLS/multi-escala + CM exporter (env mask
  BC7 con A=altura) + preview offline (EXP-011, moderna GL plane). Riesgo: LF heurística
  puede empeorar; se mantiene tras EXP-006 solo si supera umbral.
- Falsable si: EXP-006 no supera mejora ≥10% en banda LF sobre al menos 2 familias.

## S. Top 10 unknowns

1. Signo/convención exacta y universal de gradiente (por-textura puede variar si autores
   mezclaron convenciones). 2. Radio real de texturas cuyo `_n` es BC1 (info destruida).
3. Umbral de "plana" universal. 4. Comportamiento de PGPatcher con `_p` BC4 en CM-upgrade
(BC3 asumido). 5. Determinismo de texconv entre versiones. 6. Semántica de alfa de normales
moddeadas (specular? AO? vacío?). 7. Slot exacto de parallax (0-based) por tipo de material.
8. Ratio real de candidatas en una modlist típica (dimensionamiento de cache). 9. Si ENB lee
el `_p` de slot o de naming (legacy paths). 10. Interacción height blending CS con alturas
de media cero.

## T. Top 10 failure modes

1. Signo invertido global (relieve hundido) — mitigado por agreement + HITL. 2. DC mal
centrado (todo hundido/flotante) — invisible sin referencia; centrado robusto. 3. Costuras
por integración no periódica. 4. Banding 8-bit + BC4 en gradientes suaves. 5. Normales con
AO horneada → relieve "ensuciado". 6. Falsos positivos de candidata (UI, caras). 7. `_p`
que pisa pack humano (PRESERVE_EXISTING). 8. Mips que borran relieve a distancia. 9. BC4
sobre texturas no mip-able (NPOT) — rechazo temprano. 10. Crash de decode DDS exótico →
fail-isolation por textura.

## U. What would make A abandon this architecture

- Si EXP-001 muestra que el agreement de la mejor integración clásica no distingue
  reconstrucción correcta de incorrecta en texturas reales (métrica ciega).
- Si EXP-005 muestra que el signo es inestable y el volume de REVIEW humano inmanejable
  (>50% de candidatas).
- Si el rig in-game (EXP-012) muestra que la calidad percibida del `_p` clásico es
  sistemáticamente inferior a referencias humanas en >50% de materiales del benchmark.
- Si PGPatcher no acepta contratos derivables públicamente sin recurrir a conocimiento no
  público (rompería la separación de responsabilidades).

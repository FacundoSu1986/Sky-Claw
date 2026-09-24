# ARQUITECTO B — Arquitectura híbrida (determinista + IA opcional, con IA bajo tutela)

> Postura: la matemática del núcleo es la misma que la de A (y debe serlo: es la que hay);
> el diferencial de B está en los tres puntos donde la señal determinista es **débil de
> forma demostrable** — clasificación semántica de candidatas, baja frecuencia/signo de la
> altura, y evaluación perceptual del resultado. B propone IA **como proponente y crítico
> con validadores deterministas como única autoridad**, tres modos de despliegue
> (NO_AI / LOCAL_AI / REMOTE_AI) y expulsión inmediata de cualquier componente de IA que no
> pase su experimento de habilitación.

---

## A. Sources researched

Fuentes compartidas: `10_ecosystem_facts.md`. Adicionales de B:

| Claim | Fuente | Fecha | Confianza |
|---|---|---|---|
| DA3 (nov 2025): mono directo, Apache-2.0 en SMALL/BASE/MONO-LARGE; CC-BY-NC en LARGE/GIANT; ONNX comunitario | [HF DA3MONO-LARGE](https://huggingface.co/depth-anything/DA3MONO-LARGE); [DA3-ONNX con tabla de licencias](https://github.com/MoonCodeMaster/Depth-Anything-3-Onnx) | 2026-09-21 | VERIFIED |
| DA2 entrenado con 595K sintéticas + 62M **naturales** → dominio escena, no materiales | [Luxonis DA2 card](https://models.luxonis.com/luxonis/depth-anything-v2/c5bf9763-d29d-4b10-8642-fbd032236383) | 2026-09-21 | VERIFIED |
| Marigold Normals v1-1: pesos OpenRAIL++-M (restricciones); ~768px; ensemble→incertidumbre; LCM Apache-2.0 | [HF prs-eth/marigold-normals-v1-1](https://huggingface.co/prs-eth/marigold-normals-v1-1) | 2026-09-21 | VERIFIED |
| CHORD (Ubisoft, SIGGRAPH Asia 2025, pesos abiertos dic-2025): SVBRDF desde textura única; **altura derivada por integración de la normal del propio modelo**; entrenado en MatSynth; ComfyUI | [blog Ubisoft La Forge](https://www.ubisoft.com/en-us/studio/laforge/news/1i3YOvQX2iArLlScBPqBZs/generative-base-material-an-opensource-prototype-for-pbr-material-estimation-debuting-at-siggraph-asia-2025) | 2026-09-21 | VERIFIED (método); licencia de pesos NO VERIFICADA |
| MatSynth: 4,069 materiales 4K tileable CC0/CC-BY con metadata y AO/height/normal por material | [MatSynth (CVPR 2024)](https://www.researchgate.net/publication/377499353_MatSynth_A_Modern_PBR_Materials_Dataset); [overview](https://www.emergentmind.com/topics/matsynth-dataset) | 2026-09-21 | VERIFIED |
| MatE 2025: extracción de material con tileabilidad explícita ("noise rolling") | [Lacuna post](https://lacuna.tiptreesystems.com/work/mate-material-extraction-from-single-image-via-geometric-prior/wrk_00cc7ce93ef0255d2f2099598844b87e) | 2026-09-21 | NO VERIFICADO (disponibilidad) |
| Generadores PBR web (client-side classical; AI services) existentes como categoría | [genpbr.com](https://genpbr.com/generate), [3daistudio](https://www.3daistudio.com/Tools/PBRMapGenerator) | 2026-09-21 | VERIFIED (existencia) |
| PGPatcher: CM reconocida por alfa ≥50% no-blanco; prioridad PBR>CM>Parallax; Disable Pre-Patched Materials | [PGPatcher wiki](https://github.com/hakasapl/PGPatcher/wiki/Patchers), [Mod Window](https://github.com/hakasapl/PGPatcher/wiki/Mod-Window) | 2026-09-21 | VERIFIED |

## B. Current Skyrim parallax model (visión desde B)

Idéntica lectura de contratos que A (`10_ecosystem_facts.md`); B coincide en:
`_p` BC4 legacy primero, CM después, terrain y TruePBR diferidos. B añade una consecuencia
que A subraya menos: dado que PGPatcher **ya resuelve meshes y elige shader por disponibilidad
de texturas**, el generador nativo puede alterar el shader path efectivo de toda una modlist
al producir alturas (parallax activará donde antes había default). Eso convierte la
**clasificación de candidatas en una decisión perceptual de alcance global** — argumento
central de B para llevar señales semánticas (y por tanto, potencialmente IA) a esa etapa.

## C. Problem decomposition

Igual que A (E0..E9) con dos extensiones:

```
E2b AI-candidate-advisor  → probabilidad semántica de material + explicación (opcional)
E7b AI-height-proposal    → HeightProposal alternativo (solo LOCAL/REMOTE, opcional)
E8b AI-critic             → ranking perceptual de candidatos renderizados (opcional)
```

Regla estructural: **todo flujo con IA termina en los mismos validadores deterministas de A.**
La IA nunca escribe DDS, nunca decide exclusiones sola, nunca entra al output sin gates.

## D. Proposed architecture

```
EffectiveAssetProvider
      ▼
TextureInspector ─► RuleClassifier (determinista, igual que A)
      ▼                        ▲
CandidateClassifier     AiAdvisor (MODE≥1) ── solo sugiere prob+reason,
      ▼                        │          nunca excluye sola
HeightGenerator ◄─────────────┘
   ├─ NORMAL_INTEGRATION (determinista, núcleo compartido con A)
   ├─ HYBRID_CLASSICAL (LF fusion determinista)
   └─ AI_DEPTH (MODE 1/2, opcional) → produce HeightProposal
      ▼
CandidateScorer (AI-critic, opcional) sobre previews renderizados
      ▼
QualityValidator (determinista) ─► selección ─► DdsExporter ─► ManagedOutput ─► Manifest
```

Modos:
- **MODE 0 NO_AI**: pipeline exactamente = A. Es el modo de release. Todo B se apaga.
- **MODE 1 LOCAL_AI**: ONNX Runtime (DirectML en Windows, CPU fallback). Modelos pequeños
  (DA3-S/BASE ~25-100M params; clasificador ViT-S/conv pequeño; VLM local opcional tipo
  Qwen-VL 2-4B quantizado para crítico). Sin red, sin coste, sin privacidad. Requiere 2-6 GB
  VRAM o CPU con tiled inference (los ViT de DA funcionan a 518px ⇒ tiling obligatorio para
  2K/4K). [VERIFICADO resolución de entrada DA; VRAM = INFERENCIA]
- **MODE 2 REMOTE_AI**: opt-in explícito por-run con consentimiento; lista de archivos a
  subir visible antes; proveedor/version/model fijados en el manifest; deshabilitado por
  defecto; nunca para modlists completas.

## E. Algorithms

Núcleo: idéntico a `20_architect_a_determinista.md` §E (FC periódico + Poisson; convención
DirectX; confianza por nz). B no propone reemplazarlo. Extensiones:

### E.1 Fusión multi-banda determinista (HYBRID_CLASSICAL)

Los modos armónicos de baja frecuencia son irrecuperables del gradiente [Agrawal 2006].
La fusión en bandas es el mecanismo correcto para combinar fuentes complementarias:

```
H = LP_k(AI_depth_aligned) + (H_normal_int - LP_k(H_normal_int))
```

con `LP_k` = filtro gaussiano de cutoff k (o pirámide laplaciana), y `AI_depth_aligned`
reescalado por (a,b) óptimos y **elegido por signo** contra H_normal_int. La validación es
determinista: la banda HF del resultado debe conservar el `normal_agreement` del núcleo.
[HIPÓTESIS → EXP-008b] — no se declara ganador sin medir.

### E.2 AI HeightProposal (AI_DEPTH, opcional)

- Entrada: albedo (linealizado), **normal map como canal adicional** (el gradiente ya dice
  dónde están los bordes), tiling 512-518px con blending overlap-cosine, por tile; salida
  depth relativa por tile → mosaico con corrección de bordes por Poisson (reuso del solver).
- Modelos candidatos (sept-2026): DA3MONO-SMALL/BASE (Apache-2.0, ONNX), Marigold depth LCM
  (Apache) — pesos RAIL v1-1 NEEDS_REVIEW; CHORD (pesos abiertos, licencia a auditar;
  **nota**: el propio CHORD deriva altura integrando su normal — validación interna de la
  tesis de A); fine-tune propio sobre MatSynth (CC0) como path de investigación (§34 brief).
- Semántica: profundidad de escena ≠ altura de material. Se registra como **riesgo
  principal** y por eso esta vía es *proposal*, no output. [VERIFICADO el dominio de
  entrenamiento; HIPÓTESIS el fallo sobre materiales]

### E.3 AI critic (CandidateScorer)

- Render offline del height candidate (shader POM simplificado en moderngl/OpenGL 3.3, luz
  direccional + omnidireccional, cámara 3 ángulos fijos; determinista por seed) → 3 imágenes.
- Un VLM local pequeño (o REMOTE opt-in) **ordena** candidatos {A=integración, B=fusión LF,
  C=invertido, D=flat} por plausibilidad de relieve y artefactos, y **justifica** en texto
  (explicabilidad). Su salida entra como `advisory_score` con `confidence`; la autoridad de
  aceptación sigue siendo la tabla de gates de A (§I de A). Falsificación: EXP-009 mide si
  el ranking del VLM correlaciona con ground truth sintético mejor que las métricas
  deterministas solas. Si no correlaciona → se elimina la etapa.

### E.4 Tabla de responsabilidades de IA (§11 del brief)

| Uso | Input | Output | Confianza | Timeout | Fallo | Fallback | Privacidad | Coste | Reproducibilidad |
|---|---|---|---|---|---|---|---|---|---|
| Advisor candidatas | albedo+normal thumbnails | prob(material)+reason | softmax, mín 0.6 | 2 s/tile CPU | SKIPPED→reglas | reglas puras | local (M1) | 0 | alta (ONNX fp32 determinista por build) |
| HeightProposal | albedo+normal tiles | depth float por tile | ensemble std | 30 s/4K | descarta proposal | núcleo clásico | local | 0 | media (registrar pesos+OP set; fp16 puede no ser bit-exacto) |
| Critic | 3 renders | ranking+texto | logprobs/votos | 10 s | score neutro | gates solos | local/remoto opt-in | 0/remoto | baja-media (temperatura 0, seed fija) |

## F. Candidate classification

B acepta las 8 señales de A como **capa base** y añade la capa semántica opcional:

- MODE 0: idéntico a A.
- MODE 1+: clasificador de material (stone/brick/wood/fabric/metal/ground/rock/tiles/
  decal/UI/skin) entrenado **solo con datos licenciables** (MatSynth CC0 + etiquetas
  incluidas en su metadata [VERIFICADO]). Sirve para: (1) exclusiones explicables
  (`unsupported_material: skin`, `UI_texture`), (2) presets de escala/ganancia por familia,
  (3) ranking de riesgo de REVIEW. **La IA propone exclusión; la tabla determinista de
  razones decide**; una exclusión sin reason code determinista que la respalde es rechazada
  por diseño.
- Comparativa: filename (frágil) < inspection (ciego semánticamente) < NIF-graph (costoso,
  y PGPatcher ya lo hace mejor) < híbrido reglas+AI (lo que B propone si MODE≥1).

## G. Height reconstruction

Núcleo compartido con A (§G de A). Adiciones de B: fusión E.1 y proposals E.2 — siempre
terminando en `normal_agreement`, `seam_score` y la elección de signo **determinista**. La
medida de "mejor candidato" en MODE≥1 es: gates primero, luego `advisory_score` como
desempate; en MODE 0, solo gates.

## H. DDS / export strategy

Igual que A (texconv MIT como DYNAMIC_EXTERNAL_TOOL; Pillow BC3/BC5 fallback; BC4 para `_p`;
BC7/BC3 para CM; mips float propios). B no propone cambios: la única adición es registrar en
el manifest `model_fingerprint` cuando un HeightProposal participó (aunque el export sea
determinista, la reproducibilidad del contenido cambia de nivel — §M).

## I. Validation / quality gates

Los gates de A (§I de A) son la autoridad en todos los modos. B añade:

- `proposal_agreement`: agreement del proposal vs integración (si difieren >X en banda HF,
  el proposal se descarta — la IA está alucinando).
- `ensemble_dispersion` (difusión/ensemble): si la dispersión entre seeds supera τ, marcar
  `REVIEW_RECOMMENDED` (no determinista ⇒ no AUTO_ACCEPT).
- `critic_consistency`: dos renders con seeds distintas deben dar el mismo ranking; si no,
  advisory ignorado.
- Umbrales derivados del benchmark (MatSynth CC0 + sintéticos), nunca fijados a mano.

## J. Performance

- Núcleo: igual que A (CPU, <1 s/2K).
- MODE 1: DA3-S a 518px ≈ decenas de ms/tile en GPU modesta (GTX 1060/RTX 2060 clase),
  segundos/tile en CPU; 4K = 64 tiles ⇒ ~2-5 min/4K CPU, ~10-30 s GPU. VLM crítico: inviable
  CPU para volumen; solo subconjunto REVIEW. [INFERENCIA; se mide en EXP-008]
- VRAM objetivo ≤4 GB (quantización INT8/FP16, ONNX Runtime DirectML; fallback CPU).
- Presupuesto: MODE 1 en una modlist de 5K candidatas 2K ⇒ horas (GPU) a días (CPU); por eso
  MODE 1 se aplica solo a la cola REVIEW y a muestreos, no a todo el run, hasta medir.
- Cache: hash incluye model fingerprint; las proposiciones se cachean por tile.

## K. Sky-Claw integration

Mismo layering que A (`EffectiveAssetProvider → … → Manifest`), con:

- `AiAdvisor` como **plugin condicional**: solo se carga si MODE≥1 y ONNX Runtime está
  presente; su ausencia degrada a MODE 0 sin error (fail-open a reglas, no fail-closed).
- Los modelos/weights viven **fuera del repo** (descarga gestionada con hash pinneado en el
  manifest de Sky-Claw; nunca commit de pesos); terceros licencias auditadas en §N.
- HITL: la cola REVIEW existente muestra preview A/B (con/sin proposal) y el reason de IA en
  lenguaje natural; el usuario decide; el resultado se registra como feedback para calibrar
  umbrales.
- Cloud (MODE 2): banner de consentimiento explícito por run + diff de archivos a subir +
  proveedor fijo en config; sin consentimiento, bloqueado.

## L. AI role

Dónde B cree que la IA agrega valor real (cada uno con experimento de habilitación):
1. **Clasificación semántica** (EXP-007) — valor alto si el ROC de reglas (EXP-004) deja
   huecos; medible.
2. **LF/height proposal** (EXP-008) — valor incierto; OOD probado por diseño de datasets;
   el candidato serio es un modelo entrenado en dominio material (CHORD o fine-tune
   MatSynth), no depth naturalista.
3. **Crítico perceptual** (EXP-009) — valor si y solo si su ranking supera a las métricas
   deterministas correlacionando con GT; si no, sobra.
4. Explicación de exclusiones — valor UX, riesgo bajo, sin autoridad.
Y donde NO: formato DDS, integración matemática, gates, output final, exclusión final.

## M. Clean-room compliance

Idéntica política que A: nada de ParallaxR (código/strings/exclusiones/presets). Los modelos
de IA son de terceros con pesos públicos y licencias propias: se auditan (§N) y se registran
fingerprints. La extracción de *strings* de binarios de terceros para inferir comportamiento
sigue prohibida también para modelos (se usa solo documentación y pesos públicos).

## N. License / dependency audit

| Componente | Licencia | Clasificación |
|---|---|---|
| onnxruntime | MIT | SAFE_TO_DEPEND |
| torch (solo build/export; runtime ONNX evita torch en el app) | BSD-3 | OPTIONAL (build-time) |
| DA3 SMALL/BASE/MONO-LARGE, DA2 S/B/L | Apache-2.0 | SAFE_TO_DEPEND (pesos; attribution NOTICE) |
| DA3 LARGE/GIANT | CC-BY-NC-4.0 | INCOMPATIBLE con uso distribuido Sky-Claw (MIT) → excluidos |
| Marigold depth/normals v1-1 | pesos OpenRAIL++-M | NEEDS_REVIEW (restricciones de uso/redistribución; LCM variant Apache-2.0 como alternativa) |
| CHORD (Ubisoft) | pesos abiertos, licencia no verificada | NEEDS_REVIEW hasta auditar LICENSE |
| MatSynth | CC0 95% / CC-BY resto | SAFE_TO_DEPEND (benchmark; CC-BY con attribution) |
| Qwen-VL (2-8B) crítico local | Apache-2.0 (familia Qwen) | OPTIONAL, NEEDS_REVIEW por tamaño [NO VERIFICADO modelo exacto] |
| opencv/scipy/numpy/Pillow | BSD/Apache/MIT-CMU | SAFE_TO_DEPEND |

## O. Experiments (los 12, versión B)

B asume EXP-001..006 de A tal cual (son prerrequisitos de cualquier arquitectura). Añade y
refina:

- **EXP-007 Clasificador candidata/no-candidata + familia.** H: "reglas (EXP-004) dejan
  FPR>5% en el rango 90-99% recall; un clasificador CC0-trained la baja <2%". D: MatSynth
  etiquetas + texturas pl/UI/flat reales. M: ROC/PR por clase. Fallo ⇒ capa IA clasificador
  eliminada (MODE 0 permanente).
- **EXP-008 Depth naturalista vs dominio-material.** H1: "DA2/DA3 sin fine-tune produce
  alturas con correlation Spearman <0.4 vs GT de MatSynth en materiales tiled" (OOD).
  H2: "CHORD/fine-tune MatSynth supera 0.6". M: Spearman por bandas + normal_agreement.
  Fallo H1 confirmado ⇒ descartar depth naturalista; fallo también H2 ⇒ AI generation
  eliminada de la arquitectura.
- **EXP-008b Fusión de bandas.** H: "LP fusion (k calibrado) mejora RMSE LF ≥10% sin
  degradar HF" solo cuando el proposal pasa proposal_agreement. Fallo ⇒ HYBRID eliminado.
- **EXP-009 Crítico.** H: "ranking VLM de 4 candidatos alineado con GT ≥85%; consistencia
  entre seeds 100% (temperatura 0)". D: previews de EXP-001. Fallo ⇒ etapa eliminada.
- **EXP-010 DDS/mips**: compartido con A.
- **EXP-011 Preview offline**: rig moderngl POM; H: "correlación de métricas offline con
  veredicto in-game ≥0.7" (valida contra EXP-012).
- **EXP-012 Rig in-game controlado**: 1 celda, 20 materiales CC0, ENB vs CS, 2 capturas/luz;
  orden ciego de evaluadores humanos + métricas offline.

## P. MVP-SAFE

= MVP-SAFE de A **+EXP-007 como experimento deshabilitado por defecto** (medición de ROC de
reglas primero). Nada de IA en el release; la diferencia es que B deja la instrumentación
para medir el hueco que la IA debería cerrar.

## Q. MVP-BALANCED

- MVP-BALANCED de A + `AiAdvisor` MODE 1 (clasificador local ONNX, Apache-2.0) sobre la cola
  REVIEW + preview offline EXP-011 + EXP-008 completo (DA3 vs CHORD vs núcleo) sobre
  MatSynth.
- Criterios: MODE 0 produce el mismo output bit-a-bit que A (test de regresión); MODE 1 solo
  añade razones/scores, jamás cambia un aceptado; IA OFF = build sin onnxruntime funciona.
- Esfuerzo: +2-3 semanas sobre A. Incógnita principal: disponibilidad de pesos CHORD con
  licencia compatible.

## R. MVP-RESEARCH

- + HYBRID_CLASSICAL fusion (EXP-008b) + AI critic (EXP-009) + fine-tune propio CC0
  (normal+albedo→height, §34 brief) si EXP-008 H2 falla con modelos existentes; + MODE 2 con
  consentimiento para runs pequeños de auditoría.
- Falsable si: la fusión no supera al núcleo puro en el benchmark ciego, o el fine-tune no
  supera al núcleo+LF clásico — en cuyo caso B colapsa a MODE 0 (y eso es un resultado
  válido, no una derrota).

## S. Top 10 unknowns

1-8 = los de A (comparten núcleo). 9. Licencia real de pesos CHORD. 10. Viabilidad real de
VLM local de 2-4B en el hardware objetivo modesto para el volumen de una modlist, y si el
crítico aporta sobre métricas (EXP-009).

## T. Top 10 failure modes

1-8 = los de A. 9. **Alucinación de altura plausible-looking pero falsa** (peor que error
determinista: es convincente) — mitigado por proposal_agreement + HITL. 10. **Drift de
reproducibilidad** (fp16/GPU nondeterminismo) — mitigado por REPRODUCIBILITY_LEVEL en
manifest y gates que no dependen de bit-exactitud del proposal.

## U. What would make B abandon this architecture

- EXP-007/008/009 fallando todos ⇒ la capa IA no aporta nada medible sobre el determinista:
  B se reduce a A y lo declara públicamente.
- Que los pesos útiles queden todos en licencias NC/RAIL incompatibles ⇒ MODE 1 sin modelo
  legal no existe.
- Que el coste de mantenimiento de ONNX/quantización/tiling supere el beneficio en el
  hardware objetivo (medido, no asumido).
- Cualquier evidencia de que el crítico de IA introduzca sesgo sistemático no detectable por
  las gates deterministas.

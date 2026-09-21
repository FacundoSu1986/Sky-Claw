# SKY-CLAW NATIVE PARALLAX — SYNTHESIS C

> Selección de componentes que sobreviven a la crítica cruzada (`40_critica_cruzada.md`).
> No es una mezcla A+B: el núcleo es A con instrumentación de B, y la puerta de entrada de
> la IA queda definida por experimentos de habilitación, no por fe.

## 0. Veredicto honesto (regla 55 del brief)

[INFERENCIA con base verificada] Generar height maps **totalmente automáticos** de calidad
aceptable desde normal/diffuse **es viable solo en un dominio acotado**: texturas tiled de
superficie dura (piedra, ladrillo, madera, suelo, tejas, ornamentos rígidos) con normal map
presente y semántica de alfa conocida. Fuera de ese dominio (signo semántico ambiguo, baja
frecuencia, caras/pelaje/tejidos, decals, UI) la generación automática **no es segura** y el
diseño lo trata con SKIP/REVIEW, no con confianza. Un resultado válido de este diseño es
"X% AUTO_ACCEPT y el resto a HITL"; si X resulta pequeño en el benchmark, el producto se
reposiciona como **asistente de autoría** (batch + preview + HITL) y no como interruptor
global. El proyecto no se justifica a sí mismo: EXP-001b/005/012 deciden.

## 1. ¿Es viable?

Sí, en el dominio acotado anterior. La ruta crítica es corta y toda pública:
normal decode (formatos verificados) → FC/Poisson periódico (literatura verificada) →
métricas con GT sintético (MatSynth CC0 da GT de altura **humana** además de derivable) →
BC4 vía texconv MIT → PGPatcher hace los meshes (contratos verificados, 2.1.1 vigente).
El ecosistema 2026 (CS Extended Materials en core + ENB activos, PGPatcher dinámico) hace
que **solo la textura** sea lo que falta en una modlist — exactamente el hueco que llenamos.

## 2. Mínimo producto técnico

Un CLI por lotes, MODE 0, sin IA, que sobre la vista efectiva de una modlist:
descubre texturas → clasifica por reglas → integra normales (Poisson periódico) →
normaliza → valida gates → exporta `*_p.dds` BC4 + mips a un mod de salida propio
(`Sky-Claw - Native Parallax Output`) con manifest por archivo y cache por hash —
dejando meshes/flags a PGPatcher. Con REVIEW (HITL) para signo/plausibilidad ambigua.

## 3. Primer algoritmo a implementar

**`normal_fft_periodic_v1`** (Frankot-Chellappa periódico / Poisson periódico por FFT):
O(N log N), tileable por construcción, determinista, 40 líneas de numpy, falsable con EXP-001
en una tarde. Antes que él, el decodificador de normales con convención DirectX y verificación
por re-proyección (§E.1/G de A), porque un bug de signo contamina todo lo demás.

## 4. ¿Necesitamos IA para el MVP?

**No.** El MVP es MODE 0. La IA entra como *experimento instrumentado* (EXP-007/008/009)
con datos CC0, y cada uso debe demostrar sobre el benchmark que supera a las reglas/métricas
deterministas antes de habilitar un modo. La síntesis adopta la tabla §E.4 de B como contrato
de cualquier componente IA futuro (input/output/confianza/timeout/fallback/privacidad/coste/
reproducibilidad).

## 5. Dónde ofrece IA la mayor mejora (ordenado por valor esperado/riesgo)

1. **Clasificación semántica de candidatas** (si EXP-004 deja FPR alto): riesgo bajo,
   fallo barato, datos CC0 listos (MatSynth con metadata).
2. **Explicación de exclusiones/REVIEW** para HITL: UX puro, sin autoridad.
3. **LF fusion** con modelos entrenados en dominio-material (CHORD si su licencia lo
   permite, o fine-tune CC0 propio): la única vía de generación con evidencia favorable
   (el propio CHORD integra su normal — tesis del núcleo confirmada).
4. **Crítico perceptual** (EXP-009): solo si bate a las métricas deterministas en
   correlación con veredicto humano; caso contrario, sobra.
5. **Depth naturalista crudo (DA2/DA3 sin fine-tune)**: descartado como generador; se usa
   como control negativo del EXP-008.

## 6. Representación intermedia

**`HeightField`** (hipótesis 46 del brief — sobrevive; refutación fallida):

```
HeightField {
  height: float32[H,W]        # media cero, normalizada p2..p98 → [0,1] al exportar
  confidence: float32[H,W]    # |nz| × calidad local
  tileable: bool              # integración periódica vs Neumann
  algorithm_id/version, params_hash, provenance(assets+SHA256), warnings[]
}
```

Motivos que sobreviven a la crítica: (a) los tres contratos Skyrim de 2026 (`_p` rojo,
env-mask alfa CM, futuro displacement TruePBR) son **el mismo campo con distinto
encajonado**; (b) la IA futura solo puede producir `HeightProposal` (del mismo tipo) que
compite por gates — nunca DDS; (c) cache/audit/reproducibilidad operan sobre el DTO.
Exporters previstos: `LegacyParallaxExporter` (BC4, primero), `ComplexMaterialExporter`
(env mask BC7/BC3 con A=altura; después), `TerrainAlphaExporter` (difuso alfa, 127-neutro;
diferido), `TruePbrExporter` (JSON+`pbr/`; diferido).

## 7. Primer exporter Skyrim

`LegacyParallaxExporter` → `*_p.dds` BC4_UNORM + mips, registrado en el **slot de parallax**
por PGPatcher (patcher `parallax`, verificado). Razones: contrato unicanal más simple;
formato de 1 canal (BC4) barato; válido ENB y CS a la vez; naming por prefijo ya resuelto por
PGPatcher; y el riesgo de romper material existente es el mínimo de toda la tabla de §1.2.
CM/terrain tocan canales de texturas existentes (más riesgo de colisión semántica) → fase 2.

## 8. Cómo medimos calidad

Tres anillos, de barato a caro (todos automatizables):

1. **Sintético derivado**: H→normal→(pipeline)→H′; RMSE/SSIM/gradiente tras alineación
   (a,b) óptimos + mejor signo — alineación por mínimos cuadrados sobre la diferencia y
   comparación de ambos signos, para neutralizar la ambigüedad conocida [§25 del brief;
   Agrawal 2006 explica por qué es obligatorio].
2. **Humano authored (EXP-001b)**: contra height maps publicados por autores CC0
   (PolyHaven/AmbientCG vía MatSynth). Mide la brecha perceptual, no solo la matemática —
   corrección a la crítica B→A-5.
3. **In-game rig (EXP-012)**: 20 materiales CC0, cámara/luz fijas, ENB y CS, evaluación
   ciega + métricas offline correlacionadas (valida el preview de EXP-011).

Gates AUTO_ACCEPT/REVIEW/REJECT derivados de los percentiles de estos benchmarks
(NUNCA fijados a mano), con **REVIEW_RATE** reportado por run como métrica de salud.

## 9. Primer experimento

**EXP-001 + EXP-002 juntos** (sintético + tileabilidad): es el test de fuego del núcleo, no
necesita DDS, ni rig, ni IA, ni permisos, y produce la calibración de los primeros thresholds.
En la misma semana: EXP-005 mecánico (signo) para cerrar la convención DirectX.

## 10. Qué NO construiremos todavía

- Nada de IA en release (ni MODE 1): solo instrumentación de experimentos.
- No terrain parallax (alfa de difuso con semántica dual y riesgo de romper landscapes).
- No TruePBR exporter, no multi-layer, no hair/fabric/skin.
- No GUI (CLI + cola HITL existente de Sky-Claw).
- No modificación de NIF jamás (PGPatcher).
- No escritura BC4/BC7 propia (texconv externo); no optimizar Rust/GPU antes de perfilar
  (§37 del brief).
- No nombre definitivo ("Native Parallax Output" es working name).

## Matriz de decisión (sin scores arbitrarios)

Criterios del brief §54; evidencia en vez de número: **+/o/−** con justificación.

| Criterio | D1 normal-only | D2 normal+heurísticas | D3 normal+albedo clásico | D4 clásico+IA crítica/advisory | D5 IA genera altura |
|---|---|---|---|---|---|
| Calidad esperada (HF) | + exacta en HF | + ídem | + ídem | + ídem (núcleo igual) | o [EXP-008 abierto] |
| Calidad (LF/signo) | − DC imposible | − heurística débil | o EXP-006 | o gates+HITL guiados | o si dominio-material |
| Determinismo | + | + | + | + (IA solo advisory) | − [A→B-3] |
| CPU/GPU/VRAM | CPU | CPU | CPU | CPU (+ONNX opcional) | GPU recomendada |
| Complejidad | mínima | baja | media | media-alta | alta |
| Testabilidad | + GT sintético | + | + | + (IA testea vs GT) | − OOD |
| Explicabilidad | + | + | + | + (texto de advisory) | − |
| Riesgo dependencias | mínimo | mínimo | mínimo | medio (pesos/licencias) | alto [A→B-4] |
| Riesgo licencias | ninguno | ninguno | ninguno | manejable (Apache-2.0 only) | alto (NC/RAIL) |
| Offline | sí | sí | sí | sí (MODE 1) | parcial |
| Riesgo Skyrim compat | mínimo (BC4+PGPatcher) | ídem | ídem | ídem | ídem (mismo export) |

**RECOMMENDED FOR FIRST EXPERIMENT**: **D3 con instrumentación D4** — es decir, el núcleo
determinista con heurística LF acotada (EXP-006 decide si sobrevive) y la instrumentación
para los experimentos de IA. Maximiza información por unidad de trabajo: cada experimento
posterior habilita o elimina un bloque entero sin tocar el núcleo. D5 solo como investigación
(no release) y con modelos de dominio-material licenciables.

## Roadmap por PRs (derivado de la investigación; nombres propios, no del brief)

| PR | Goal | Write-set conceptual | Depende de | Tests / exit criteria |
|---|---|---|---|---|
| **NP-R0** | Adoptar este doc como design brief + CLEAN_ROOM.md + decisión de working name | docs/design/specs (nuevo spec v1), CLEAN_ROOM.md | — | aprobación del operador |
| **NP-D0** | Dataset spec + generator sintético (H→normal matemático, ruido, BC) + fixture MatSynth CC0 subset (descarga externa, hash-pinned, nunca commiteada) | `native_parallax/bench/` | NP-R0 | EXP-001 dataset listo; reproducible por hash |
| **NP-C0** | `NormalDecoder` (DDS read Pillow+bcdec path; convención DirectX; confianza nz) puro | `native_parallax/decode.py` | NP-R0 | unit tests con fixtures DDS (BC1/BC5/BC7/uncompressed); EXP-003 |
| **NP-I0** | `normal_fft_periodic_v1` + variante Neumann DCT + normalización percentil | `native_parallax/integrate.py` | NP-C0 | EXP-001 verdes (RMSE<0.05 limpio), EXP-002 seam<0.01 |
| **NP-Q0** | `metrics.py` (tabla §I de A) + gates con thresholds **derivados** del benchmark y versionados | `native_parallax/metrics.py` | NP-I0 | gates estables en corpus; REVIEW_RATE reportado |
| **NP-X0** | Experimentos signo/escala/inversión (EXP-005) + reporte de REVIEW_RATE simulada | `native_parallax/experiments/` | NP-Q0 | accuracy flip ≥95%; decisión documentada sobre REVIEW |
| **NP-E0** | `DdsExporter` — wrapper texconv (DYNAMIC_EXTERNAL_TOOL, registro en tool registry de Sky-Claw, version pin) + mips float propios + fallback Pillow BC3 | `native_parallax/export.py`, `local/tools/` | NP-Q0 | EXP-010; BC4 byte-identical entre runs del mismo texconv (o tolerancia documentada) |
| **NP-O0** | `ManagedOutput` (mod propio, PRESERVE_EXISTING, staging→swap) + Manifest (SHA256, algorithm_id, params, encoder version, REPRODUCIBILITY_LEVEL) + hash cache | `native_parallax/output.py` | NP-E0 | rebuild idempotente: misma cache key ⇒ skip; 0 overwrites de fuente |
| **NP-A0** | `AssetProvider` interfaz + adapter MO2 (VFS ya existente en Sky-Claw) + runner batch con fail-isolation por textura (DTO de estado) | `native_parallax/provider.py`, adapter | NP-O0 | tests contra carpeta plana (sin MO2) y contra fixture MO2 |
| **NP-V0** | Preview offline (moderngl POM plane, 3 luces fijas, seeds deterministas) | `native_parallax/preview.py` | NP-Q0 | EXP-011: correlación con EXP-012 ≥0.7 |
| **NP-RIG** | Rig in-game controlado (20 CC0, ENB y CS, evaluación ciega) | `docs/validation/native-parallax-rig/` | NP-A0,NP-V0 | EXP-012; go/no-go del dominio acotado |
| **NP-H0** | HITL: cola REVIEW con preview A/B + reason codes; curación de familias | Sky-Claw HITL lane | NP-X0,NP-V0 | REVIEW_RATE<15% en corpus o justificación |
| **NP-AI0** | Instrumentación EXP-007 (clasificador ONNX Apache-2.0, OFF por defecto) | `native_parallax/ai/` (opcional import) | NP-H0 | ROC reportado; habilitación solo si FPR<2% |
| **NP-AI1** | EXP-008/008b (DA3 control negativo; CHORD si licencia OK; fusión LP) | ídem | NP-AI0 | decisión documentada de HYBRID/AI_DEPTH |
| **NP-AI2** | EXP-009 crítico (VLM local sobre previews) | ídem | NP-V0 | habilitar solo si bate métricas vs humano |
| **NP-CM** | `ComplexMaterialExporter` (env mask BC7/BC3 con A=altura, gloss/metal conservados) | `native_parallax/export_cm.py` | NP-RIG | gates CM + rig CM |
| **NP-DOC** | Actualizar P0/v3: nuevo producer en el grafo Pre-LOD Materials, posición relativa a PGPatcher y TexGen/DynDOLOD | docs/design/plans v4 | NP-O0 | reconciliación con §8 del P0 |

Cada PR es chico, con write-set acotado, sin acoplarse a los lanes DynDOLOD/Runtime-Vault;
el único punto de contacto con Sky-Claw es tool registry (texconv, PGPatcher ya registrado),
HITL y output ownership — los tres contratos ya existentes en el repo.

## Respuesta final a la pregunta de negocio (§54)

Con la evidencia actual, la opción que maximiza información por unidad de trabajo es
construir **el núcleo determinista MODE 0 y el benchmark** (PRs NP-R0..NP-X0, ~2-4 semanas
de esfuerzo estimado), porque: (1) no se puede evaluar ninguna IA sin ese baseline;
(2) la crítica cruzada muestra que los beneficios IA reales hoy están en clasificación y
crítica, no en generación; (3) el estado del arte material (CHORD) confirma la altura-por-
integración como método de referencia; y (4) si EXP-001b/012 demuestran que el resultado
clásico no alcanza calidad perceptual, el mismo HeightField + HITL se convierte en el
asistente de autoría — el proyecto pivota sin perder la infraestructura.

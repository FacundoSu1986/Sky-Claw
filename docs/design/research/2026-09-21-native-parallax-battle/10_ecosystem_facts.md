# 1. Qué significa "parallax" en Skyrim a septiembre de 2026 — hechos verificados

Documento de referencia compartido por los arquitectos A y B y la síntesis C.
Cada fila lleva estado epistémico y enlace. Nada de lo que sigue proviene de
ParallaxR (solo se registra su existencia como categoría, Nexus mod 124711,
ya documentado en el P0 del repo).

## 1.1 El ecosistema de shading: quién implementa qué

| Hecho | Estado | Fuente |
|---|---|---|
| Skyrim SE vanilla **no ejecuta parallax correctamente**: los shaders de parallax faltan/están rotos; el mod "SSE Parallax Shader Fix" (aers, 2020) los inyectaba, y hoy está **obsoleto e incompatible** con ENB y Community Shaders | [VERIFICADO] | [SSE Parallax Shader Fix (Nexus 31963)](https://www.nexusmods.com/skyrimspecialedition/mods/31963) — sticky del autor y comentarios 2024: "Parallax Shader Fix was a great mod, but it is incompatible and obsolete with ENB or Community Shaders" |
| El parallax hoy lo implementan dos stacks excluyentes entre sí: **ENB** y **Community Shaders (CS)**. No se usan juntos | [VERIFICADO] | [r/skyrimvr — Community Shaders/Parallax?](https://www.reddit.com/r/skyrimvr/comments/1bull9p/community_shadersparallax/): "The two are not compatible. Use CS or ENB, not both" |
| ENB activa parallax por `enbseries.ini [EFFECT]`: `EnableComplexParallax`, `EnableComplexParallaxShadows`, `EnableTerrainParallax`, `EnableComplexTerrainParallax`, `EnableComplexTerrainParallaxShadows` | [VERIFICADO] | [SSE Parallax Shader Fix — comentarios](https://www.nexusmods.com/skyrimspecialedition/mods/31963?tab=posts&BH=0): "I don't need this for AE. Just activate this in the enbseries.ini: EnableTerrainParallax=true …" (ENB 0.502, 2024) |
| En Community Shaders, la feature "Complex Parallax Materials" (doodlum, mod 95134, 2024) fue **renombrada "Extended Materials"** en CS 1.0.0 (2025-01) y es hoy **parte del core de CS**; ya no se instala aparte | [VERIFICADO] | [Complex Parallax Materials — posts](https://www.nexusmods.com/skyrimspecialedition/mods/95134?tab=posts): sticky 2025-01-26 "Per CS 1.0.0 Release Notes 'Renamed Complex Parallax Materials to Extended Materials'"; sticky 2025-07-21 "Now part of Community Shaders core" |
| Extended Materials (CS core) expone: `EnableComplexMaterial=1`, `EnableParallax=1`, `EnableTerrain=0`, `EnableHeightBlending=1`, `EnableShadows=1`, `ExtendShadows=0`, `EnableParallaxWarpingFix=1`. Si `EnableTerrain` está on, fuerza `bLandSpecular:Landscape=true` (el canal specular del terreno carga la altura) | [VERIFICADO] | [DeepWiki — Extended Materials (refleja src de doodlum/skyrim-community-shaders)](https://deepwiki.com/doodlum/skyrim-community-shaders/5.1-extended-materials-and-parallax); [Nexus — Extended Materials article](https://www.nexusmods.com/skyrimspecialedition/articles/9413) |
| CS implementa POM moderno ("Contact Refinement Parallax Mapping") + sombras aproximadas + height blending del terreno (mezcla hasta 6 capas por altura) | [VERIFICADO] | [Nexus 95134](https://www.nexusmods.com/skyrimspecialedition/mods/95134) (descripción original CRPM); [DeepWiki ExtendedMaterials.hlsli](https://deepwiki.com/doodlum/skyrim-community-shaders/5.1-extended-materials-and-parallax) |
| TruePBR es un framework PBR para CS: rutas `textures/pbr/...`, JSON de configuración por material (`PBRNIFPatcher/*.json`), con displacement/parallax propio y escala (`displacement_scale`) | [VERIFICADO] | [ThePagi/PBRNifPatcher (GitHub)](https://github.com/ThePagi/PBRNifPatcher); [TruePBR Manager (Nexus 180451)](https://www.nexusmods.com/skyrimspecialedition/mods/180451) |
| ENB y CS son mutuamente excluyentes; cualquier decisión de output debe ser válida en ambos o elegirse por stack | [INFERENCIA] (de las dos filas anteriores) | — |

## 1.2 Dónde vive la altura (contratos de textura)

| Variante | Portador de la altura | Semántica de valores | Formato/compresión observada | Estado |
|---|---|---|---|---|
| **A. Vanilla/legacy object parallax** (ENB "simple" + CS `EnableParallax`) | Textura height map dedicada `*_p.dds`, en el **slot de parallax del texture set**; el shader lee **el canal rojo** | Escala de grises; altura relativa, 8-bit | BC4 adoptado por un pack grande (v5.0.0: "Object parallax maps now ship as **BC4** instead of BC7. For a single-channel height map this is visually identical at roughly half the file size") | [VERIFICADO] — [aers, Nexus 31963 notas de autor](https://www.nexusmods.com/skyrimspecialedition/mods/31963): "The parallax map texture goes in the 4th slot in the texture set, and the height is read from the first (red) channel"; [mod 125527](https://www.nexusmods.com/skyrimspecialedition/mods/125527) |
| **C/D. Complex Material / Complex Parallax** (ENB + CS `EnableComplexMaterial`) | **Canal alfa de la environment mask** (`*_m.dds`/`*_em.dds`); R=env mask, G=glossiness, B=metalness, **A=height** | ENB guide: "The heightmap goes into the alpha channel"; alpha **255 (blanco) = plano y no computado**; alpha **0 (negro) = sin parallax**; rango intermedio = parallax activo | Se distribuye como BC3/BC7 (el generador de env masks de PGPatcher guarda **BC3**); DEBE conservar alfa ⇒ **BC5 inválido para este rol** | [VERIFICADO] — [ENB Complex Material guide for creators (mirror Schaken del artículo de Nexus)](https://schaken-mods.com/forums/topic/81106-enb-complex-material-guide-for-creators/); [PGPatcher wiki — Patchers](https://github.com/hakasapl/PGPatcher/wiki/Patchers): "Generates a new environment mask texture … The alpha channel is set to the R … of the matching parallax height map. This texture is saved BC3"; BC5 sin alfa: [BCn en Wikipedia/DirectX spec](https://learn.microsoft.com/en-us/windows/win32/direct3d10/d3d10-graphics-programming-guide-resources-block-compression) |
| **E. Terrain parallax** (ENB `EnableTerrainParallax` + CS `EnableTerrain`) | **Canal alfa del diffuse** del landscape | **127 = plano/neutral** ("Pixels with brightness (value) of 127 are flat"); NO es un switch: se computa igual | BC7 (los packs con "blending fix" mantienen BC7 porque el alfa del diffuse carga profundidad); el terrain necesita **todas** las texturas de landscape con alfa válido ("requires all terrain textures to support parallax") | [VERIFICADO] — [ENB guide (mirror)](https://schaken-mods.com/forums/topic/81106-enb-complex-material-guide-for-creators/); [DeepWiki Extended Materials](https://deepwiki.com/doodlum/skyrim-community-shaders/5.1-extended-materials-and-parallax); [Parallax Fix Project — Landscape (Nexus LE 61452)](https://www.nexusmods.com/skyrim/mods/61452?tab=docs): "Terrain parallax uses the alpha channel of the diffuse texture"; [mod 125527](https://www.nexusmods.com/skyrimspecialedition/mods/125527): "Terrain blending-fix textures stay BC7, since they carry depth in the diffuse alpha channel" |
| **F. TruePBR displacement** | Textura dedicada `*_p.dds` bajo `textures/pbr/...` + `displacement_scale` en JSON | Escala de grises, escala configurable por material | Definido por el toolchain (BC4/BC7 según slot) | [VERIFICADO] — [ThePagi/PBRNifPatcher](https://github.com/ThePagi/PBRNifPatcher): "Parallax (height, displacement): texturename_p.dds"; [TruePBR Manager](https://www.nexusmods.com/skyrimspecialedition/mods/180451) tabla de slots |
| G. ENB legacy (LE-style) | idem A para meshes; idem E para terreno | idem | — | [VERIFICADO] (mismas fuentes) |

Notas de la tabla:

- **Cuantización 8-bit importa**: "these textures are stored with 8-bit per pixel precision,
  allowing only 256 possible height values"; la banda y el aliasing de la cuantización son un
  riesgo documentado y por eso un pack grande elige **menor resolución** para los `_p`
  [VERIFICADO — mod 125527].
- La denominación "Complex Parallax" == "Parallax" en PGPatcher: "Also called Complex Parallax
  sometimes (They are the same thing). … it only requires a parallax height map texture
  (usually _p.dds suffix)" [VERIFICADO — PGPatcher wiki Patchers]. Es decir: el nombre
  comercial "complex parallax" de PGPatcher se refiere al parallax de objeto con `_p.dds`,
  **no** al Complex Material. Dos vocabularios conviven y hay que desambiguar siempre.

## 1.3 Meshes: por qué PGPatcher existe y qué delega bien

| Hecho | Estado | Fuente |
|---|---|---|
| El parallax legacy requiere **mesh parcheado**: shader type "parallax", flag Parallax, vertex colors habilitados (si no, el mesh se ve azul por shader faltante); no se puede combinar con glow/envmap/multilayer | [VERIFICADO] | [aers, Nexus 31963 notas de autor](https://www.nexusmods.com/skyrimspecialedition/mods/31963) |
| PGPatcher (hakasapl; 2.1.1 = Latest 2026-09-18, GPL-3) parchea el **mesh ganador** del load order según las **texturas ganadoras**: patchers shader `parallax`, `complexmaterial`, `truepbr`, transform `parallaxtocm`, post-patcher "Disable Pre-Patched Materials" | [VERIFICADO] | [PGPatcher wiki — Patchers](https://github.com/hakasapl/PGPatcher/wiki/Patchers); [DeepWiki hakasapl/PGPatcher](https://deepwiki.com/hakasapl/PGPatcher); P0 del repo (§4: releases 2.0.0→2.1.1) |
| Requisitos del parallax patcher: `_p` con prefijo que matchee diffuse **o** normal; no GRAS; sin single-pass MATO; sin havok; no skinned; **sin NiAlphaProperty**; shader default/parallax/envmap; **no decal**; sin soft/rim/back/aniso lighting; habilita vertex colors | [VERIFICADO] | [PGPatcher wiki — Patchers](https://github.com/hakasapl/PGPatcher/wiki/Patchers) |
| CM patcher: la env mask se reconoce como CM si **el alfa tiene ≥50% de píxeles no-blancos**; setea env mapping flag; specular flag si hay glossiness (canal verde >4 o meta json) | [VERIFICADO] | [PGPatcher wiki — Patchers](https://github.com/hakasapl/PGPatcher/wiki/Patchers) |
| `parallaxtocm` genera env mask nueva: RGB negro, alfa = canal R del `_p` matcheado, **guardada BC3** | [VERIFICADO] | [PGPatcher wiki — Patchers](https://github.com/hakasapl/PGPatcher/wiki/Patchers) |
| Resolución de conflictos: PGPatcher prioriza **PBR > CM > Parallax**; ventana "Mod Window" para elegir mod fuente de assets | [VERIFICADO] | [PGPatcher wiki — Mod Window](https://github.com/hakasapl/PGPatcher/wiki/Mod-Window) |
| "Auto Parallax" es un plugin SKSE de runtime (previene meshes azules cuando falta el `_p`); PGPatcher lo reemplaza offline con "Disable Pre-Patched Materials". No es un nodo de pipeline (ya resuelto en P0/v3 del repo) | [VERIFICADO] | P0 del repo (§PG5) + [PGPatcher wiki](https://github.com/hakasapl/PGPatcher/wiki/Patchers) |
| PGPatcher produce `ParallaxGen_Diff.json` (CRC32 por mesh) que DynDOLOD usa para matchear meshes re-parcheados | [VERIFICADO] | P0 del repo (§Output, wiki Output.md) |

**Conclusión de separación de responsabilidades** [INFERENCIA apoyada en las tablas 1.1–1.3]:
un generador nativo **solo necesita producir texturas** (height maps y, en su caso, env masks);
todo el estado de mesh/material (flags, slots, types) es dominio legítimo de PGPatcher.
Esta hipótesis se evalúa formalmente en la síntesis C (pregunta 6–7).

## 1.4 Normal maps en Skyrim: convenciones

| Hecho | Estado | Fuente |
|---|---|---|
| Skyrim usa normal maps **tangent-space con convención DirectX**: canal **verde invertido** respecto a OpenGL (Blender). Verde brillante = "hacia abajo", rojo brillante = derecha, azul = hacia afuera | [VERIFICADO] | [Beyond Skyrim — Arcane University: Baking](https://wiki.beyondskyrim.org/wiki/Arcane_University:Baking): "Because Skyrim uses DirectX, the normal maps should have their Y (i.e. green) channel flipped compared to the OpenGL standard"; [r/skyrimmods PSA](https://www.reddit.com/r/skyrimmods/comments/q8thwu/psa_make_sure_you_use_the_right_coordinate_system/) |
| Los **tangentes y bitangents viven por-vértice en el NIF** (interpolados en shader), de facto MikkTSpace; Blender calcula bitangent por-pixel y por eso desalinea | [VERIFICADO] | [Beyond Skyrim — Baking](https://wiki.beyondskyrim.org/wiki/Arcane_University:Baking) |
| La convención DirectX/OpenGL **no es detectable de forma general solo por píxeles** — los motores exponen flip manual, sin auto-detección | [VERIFICADO] (para la imposibilidad general) | [PyNifly issue #61 + research del PR citado](https://github.com/BadDogSkyrim/PyNifly/issues/61): "no reliable general-case, pixel-data-only detection method exists" |
| BC5 (2 canales, sin alfa) es el formato comprimido estándar para normal maps en D3D11; Z se reconstruye. BC5 **no puede** portar altura en alfa | [VERIFICADO] | [Microsoft — Block compression (BC5 = 2 canales)](https://learn.microsoft.com/en-us/windows/win32/direct3d10/d3d10-graphics-programming-guide-resources-block-compression) |
| La conversión gradiente→altura a partir de normal maps es práctica pública documentada en la comunidad Skyrim (Poisson + multi-escala, variante FFT para menos artefactos de borde) | [VERIFICADO] (existencia pública del método) | [Nexus article 7202 — "Converting Normal Maps to Height Maps Using Poisson Integration and Multi-Scale Enhancement"](https://www.nexusmods.com/skyrimspecialedition/articles/7202) |

## 1.5 Matemática de reconstrucción de altura (fuentes primarias)

| Método | Referencia | Uso en nuestro problema | Estado |
|---|---|---|---|
| Proyección integrable por Fourier ("Frankot-Chellappa") | [Frankot & Chellappa 1988, IEEE TPAMI](https://ieeexplore.ieee.org/document/1641); survey: [Quéau, Durou, Aujol, "Normal Integration: A Survey", arXiv:1709.05940](https://arxiv.org/abs/1709.05940) | P=(-nx/nz, -ny/nz) → H por FFT. "M_FC works well if and only if the surface to be reconstructed is periodic" — exactamente el caso de texturas tiled | [VERIFICADO] |
| Poisson por DCT (Neumann), no iterativo | [Simchony, Chellappa, Shao 1990](https://arxiv.org/abs/1709.05940) (citado en survey §3.2) | Dominios no periódicos; base de la integración por transformadas | [VERIFICADO] |
| Rango de las reconstrucciones Poisson (ambigüedad armónica/DC) | [Agrawal, Raskar, Chellappa, ECCV 2006, "What is the range of surface reconstructions from a gradient field?"](https://link.springer.com/chapter/10.1007/11957959_72) | Fundamenta la imposibilidad de recuperar baja frecuencia/DC desde el campo de gradientes | [VERIFICADO] (referencia; lectura del survey) |
| Frontera periódica vía Poisson (tileabilidad) | [Pérez, Gangnet, Blake 2003, "Poisson Image Editing"](https://www.cs.jhu.edu/~misha/Spring07/perez03.pdf); [INR tileable con regularización Poisson, arXiv:2402.02208](https://ar5iv.labs.arxiv.org/html/2402.02208) | "the boundary conditions consist of forcing the opposite sides of the domain to be equal" | [VERIFICADO] |
| Integración robusta / con pesos / L1 / multi-escala | [Quéau et al. survey §4](https://arxiv.org/abs/1709.05940); [Fuzzy Frankot–Chellappa, Algorithms 18(8):488, 2025](https://www.mdpi.com/1999-4893/18/8/488) (ruido y gaps, 2025) | Manejo de no-integrabilidad (cuantización, BC, AO horneada) | [VERIFICADO] (existencia y resultados cualitativos) |
| advertencia práctica: implementaciones públicas de FC con error de factor 2π | [Survey arXiv:1709.05940 §3.3](https://arxiv.org/abs/1709.05940) | Nunca copiar implementación sin tests sintéticos | [VERIFICADO] |

## 1.6 DDS / compresión / herramientas (licencias auditadas)

| Herramienta | Rol | Licencia | Estado |
|---|---|---|---|
| **DirectXTex / texconv** (Microsoft) | encode BC1-BC7, mips, conversión; CLI headless | **MIT** (winget: `Microsoft.DirectXTex.Texconv` 2026.5.7) | [VERIFICADO] — [winstall](https://winstall.app/apps/Microsoft.DirectXTex.Texconv); [GitHub GarbageCollectors? ver repo DirectXTex](https://github.com/microsoft/DirectXTex) |
| **Compressonator** (AMD GPUOpen) | CLI/SDK BC1-BC7; CPU/GPU | **MIT** (builds con bc7e Apache-2.0) | [VERIFICADO] — [fork documentado con tabla de licencias](https://github.com/noisethanks/compressonator) |
| **NVIDIA Texture Tools (nvtt)** | encode BC, CUDA opcional | **MIT** | [VERIFICADO] — [LICENSE en castano/nvidia-texture-tools](https://github.com/castano/nvidia-texture-tools/blob/master/LICENSE): "NVIDIA Texture Tools is licensed under the MIT license" |
| **bcdec** | decoder single-header BC1-BC7 | **Unlicense** | [VERIFICADO] — [comparativa de decoders BCn (Aras-P)](https://aras-p.info/blog/2022/06/23/Comparing-BCn-texture-decoders/) |
| **bc7enc_rdo / rgbcx** | encoder BC1-BC7 CPU rápido | MIT/PublicDomain | [VERIFICADO] — [Aras-P](https://aras-p.info/blog/2022/06/23/Comparing-BCn-texture-decoders/) |
| **Pillow** | decode DDS (BC1/2/3/5/7 lectura), y desde **11.2.1 (2025-04)** **escritura** DXT1/3/5, BC2, BC3, **BC5** | MIT-CMU (HPND) | [VERIFICADO] — [Pillow release notes 11.2.1](https://pillow.readthedocs.io/en/latest/releasenotes/11.2.1.html): "Compressed DDS images can now be saved … DXT1, DXT3, DXT5, BC2, BC3 and BC5 are supported" |
| Pillow | **NO escribe BC4 ni BC7** (a la fecha verificada) | — | [INFERENCIA] de la lista explícita de formatos soportados en save |
| squish | decoder BC4/BC5 **buggy** ("decoding produces wrong results") | MIT | [VERIFICADO] — [Aras-P](https://aras-p.info/blog/2022/06/23/Comparing-BCn-texture-decoders/) |
| texture2ddecoder (PyPI) | decode BCn desde Python | MIT (bindings) | [NO VERIFICADO] — pendiente auditoría de versión |

## 1.7 IA: estado del arte aplicable (sept 2026)

| Modelo/dataset | Qué hace | Licencia | Estado |
|---|---|---|---|
| **Depth Anything V2** (2024) | profundidad monocular relativa; entrenado con 595K sintéticas + **62M imágenes naturales pseudo-etiquetadas** (escenas, no materiales) | Apache-2.0 (S/B/L); Giant CC-BY-NC | [VERIFICADO] — [Luxonis model card](https://models.luxonis.com/luxonis/depth-anything-v2/c5bf9763-d29d-4b10-8642-fbd032236383); [Roboflow summary](https://playground.roboflow.com/models/bytedance/depth-anything-v2) |
| **Depth Anything 3** (ByteDance Seed, **nov 2025**) | profundidad relativa **directa** (no disparidad) + geometría multi-vista; DA3MONO-LARGE 0.35B | **Apache-2.0** para SMALL/BASE/MONO-LARGE y METRIC-LARGE; **CC-BY-NC** para LARGE/GIANT | [VERIFICADO] — [HF DA3MONO-LARGE](https://huggingface.co/depth-anything/DA3MONO-LARGE); [ONNX export comunitario con tabla de licencias por variante](https://github.com/MoonCodeMaster/Depth-Anything-3-Onnx) |
| **Marigold Normals v1-1** (2024-25) | normales monocular por difusión; resolución efectiva ~768px; ensemble → mapa de incertidumbre | pesos **OpenRAIL++-M** (restricciones de uso); variante LCM Apache-2.0 | [VERIFICADO] — [HF prs-eth/marigold-normals-v1-1](https://huggingface.co/prs-eth/marigold-normals-v1-1) |
| **CHORD / "Generative Base Material"** (Ubisoft La Forge, SIGGRAPH Asia 2025, pesos abiertos dic 2025) | difusión que predice canales SVBRDF desde UNA textura (albedo→normal→**altura por integración de la propia normal**), entrenado en MatSynth, pipeline ComfyUI, tileable | pesos abiertos; **licencia de pesos no verificada en esta sesión** | [VERIFICADO] (existencia/método) — [blog Ubisoft La Forge](https://www.ubisoft.com/en-us/studio/laforge/news/1i3YOvQX2iArLlScBPqBZs/generative-base-material-an-opensource-prototype-for-pbr-material-estimation-debuting-at-siggraph-asia-2025); licencia [NO VERIFICADO] |
| **MatE** (2025) | extracción de material desde foto única con prior geométrico; **tileable por "noise rolling"** | disponibilidad/weights no verificadas | [NO VERIFICADO] — [Lacuna post](https://lacuna.tiptreesystems.com/work/mate-material-extraction-from-single-image-via-geometric-prior/wrk_00cc7ce93ef0255d2f2099598844b87e) |
| **MatSynth** (CVPR 2024) | **4,069 materiales PBR 4K tileable**, ~95% CC0 (AmbientCG, PolyHaven, cgbookcase…), metadata por material (categoría, tags, licencia fuente) | CC0/CC-BY | [VERIFICADO] — [paper (ResearchGate)](https://www.researchgate.net/publication/377499353_MatSynth_A_Modern_PBR_Materials_Dataset); [overview con licencias](https://www.emergentmind.com/topics/matsynth-dataset) |
| Generadores PBR comerciales/free (GenPBR web client-side, 3daistudio) | imagen→normal/rough/height/AO en browser; evidencia de categoría y de demanda | servicios cerrados | [VERIFICADO] (existencia) — [genpbr.com](https://genpbr.com/generate), [3daistudio](https://www.3daistudio.com/Tools/PBRMapGenerator) |

**Hecho crítico etiquetable** [INFERENCIA fuerte, con base verificada]: los modelos de
profundidad monocular (DA2/DA3, Marigold) se entrenan con **escenas naturales** (Hypersim,
Virtual KITTI, 62M fotos); una textura **tileable** de material es **fuera de distribución**
(sin escena, sin escala métrica, con repetición periódica). No hay evidencia de que DA3
produzca alturas útiles para materiales tiled sin fine-tuning. Esto es una hipótesis a
refutar por experimento (EXP-008), no un axioma. En contraste, CHORD sí fue entrenado en
dominio material (MatSynth) y **ni siquiera predice altura directamente: la integra desde
su normal** — señal décil pero real de que el estado del arte también considera la altura
mejor derivada de normales que "imaginada" por el modelo.

## 1.8 Lo que NO pudo verificarse en esta sesión

- Índice exacto (0-based vs 1-based) del slot de parallax en `BSShaderTextureSet`/`BSLightingShaderMaterial`: aers dice "4th slot"; la numeración exacta del TX se cerrará con inspección T2 de paquete o del código PGPatcher. **[REAL_RIG_REQUIRED]**
- Licencia exacta de los pesos de CHORD. **[NO VERIFICADO]**
- Comportamiento de `EnableHeightBlending` cuando el alfa del diffuse es inválido (solo tooltips). **[NO VERIFICADO]**
- Soporte VR de parallax por stack 2026 (CS-VR vs ENB-VR): relatos de usuarios, no docs. **[NO VERIFICADO]**
- Determinismo bit-exacto de texconv/Compressonator entre versiones/hardware (para la política de reproducibilidad): a diseñar como experimento EXP-010. **[NO VERIFICADO]**

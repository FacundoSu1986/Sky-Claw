# Auditoría matemática y arquitectónica — Native Parallax (Enfoque A vs B)

> **Fecha:** 2026-10-07 · **Base:** `97dcc7a` (rama `claude/clever-hopper-a8z4gp`).
>
> **Alcance:** `sky_claw/local/native_parallax/research/`, `sky_claw/local/tools/parallaxr_assisted.py`,
> `docs/design/research/2026-09-21-native-parallax-battle/` y `docs/design/research/native-parallax/`.
>
> **Estado epistémico por afirmación:** `[EJECUTADO]` (reproducido corriendo código del repo en
> esta sesión), `[LEÍDO]` (verificado leyendo código/docs), `[INFERENCIA]`, `[NO VERIFICADO]`.
>
> **Carácter:** evidencia fechada (ver [README](README.md)). No modifica código ni resultados
> congelados de M0–M5.

## 0. Reencuadre del encargo

El encargo original hablaba de *scroll parallax*, game loop, cámara y render pipeline. Nada de
eso existe en Sky-Claw `[LEÍDO]`. Sky-Claw es un gestor offline de mods de Skyrim; el
"parallax" del repo es la **generación offline de height maps** (`*_p.dds`, alfa de env mask)
que después consume el parallax occlusion mapping de Community Shaders o ENB, cuyo shader no
vive en este repo. La traducción de cada pregunta:

| Pregunta original | Equivalente real en el repo |
|---|---|
| Acoplamiento con game loop / cámara / render pipeline | Acoplamiento del paquete `native_parallax` con la app, con el pipeline de materiales (PGPatcher, DynDOLOD) y con los exporters DDS |
| Enfoque A / Enfoque B | Arquitecto A determinista (`20_architect_a_determinista.md`) / Arquitecto B híbrido con IA (`30_architect_b_hibrida.md`) |
| `Δpos = f(pos_cámara, z_capa, factor)` | Offline: normal → gradiente → Poisson periódico. Runtime (no es nuestro): offset POM `Δuv ∝ V.xy/V.z · s · (1−h)` |
| Lineal vs recíproca en `z` | La inversa `p = −nx/nz` es hiperbólica en `nz`; el offset POM es hiperbólico en `V.z` |
| Jitter subpíxel, drift, delta time | No hay frame loop. Los análogos son unidades y aspecto, re-cuantización 8-bit, la escala implícita de la pendiente y el determinismo |

## 1. Resumen ejecutivo

- El **núcleo matemático de A es correcto** en lo que se probó: la derivación Frankot–Chellappa,
  el DC, el guard de los 4 bins nulos, Nyquist, la inversa corregida por PR-MATH-A, Tikhonov y
  el Spearman con empates `[LEÍDO + EJECUTADO: 371 tests verdes]`.
- Los defectos están en **los instrumentos de medición** y en las **convenciones de unidades
  entre hermanos**: 8 hallazgos reproducidos por ejecución (sobre datos sintéticos, salvo donde se
  indica). Uno es crítico para la validez de la evidencia (H1). H2 es una convención de aspecto que
  nadie declara: severidad alta para cualquier entrada no cuadrada, no crítica, porque el corpus
  primario de M3 es cuadrado por construcción.
- **Veredicto:** adoptar A (= Síntesis C, D3 con la instrumentación de D4). Antes de EXP-001b/012
  y de cualquier experimento de B hay que reparar los instrumentos (P0/P1), porque los criterios
  de habilitación de B se miden con esos mismos instrumentos.

## 2. Diagnóstico arquitectónico

**Estado de los enfoques** `[LEÍDO]`

| | A — determinista | B — híbrido (IA) |
|---|---|---|
| Código | Research M0–M5 en `native_parallax/research/` (~6k LOC, 371 tests verdes) | Ninguno; solo diseño |
| Producción | Ninguna (`__init__`: "Nada de este paquete es código productivo") | Ninguna |
| Integración actual | Solo el handoff manual de ParallaxR (`parallaxr_assisted.py`, read-only, sin subprocesos) | — |

**Acoplamiento**

- *Interno — bueno.* Los módulos research solo importan entre sí. `grep` sobre
  `native_parallax/research` no muestra imports de `sky_claw.app` `[LEÍDO]`.
- *Raíz del paquete — malo.* `sky_claw/__init__.py` importa de forma eager `sky_claw.local.assets`,
  que arrastra `app.security` → `app.core.database` → `aiosqlite`. Un módulo que se declara
  "PURO (solo numpy)", como `frequency_coherence.py`, no se puede importar sin la app entera.
  Reproducido: `ModuleNotFoundError: No module named 'aiosqlite'` `[EJECUTADO]`.
- *Provenance congelado.* M4 registra `solver_module_sha256` y M5 ata su freeze al checkout del
  módulo `[LEÍDO]`. Cualquier corrección del solver **debe versionarse** (`…_v2`), nunca
  editar v1 en sitio.
- *Separación con PGPatcher.* Es correcta y ya está decidida: el generador solo produce
  texturas y nunca toca NIF.

**Deuda técnica**

- Tres convenciones de unidades conviven en cuatro módulos (ver H2/H5): `freq_axes`
  (rad/unidad UV por eje), `band_errors`/`_band_energy` (ciclos/muestra), `rho_grid`
  (ciclos/tile por eje) y `fd_forward` (por muestra).
- *Drift documental.* El docstring de `freq_axes` dice `wx = 2π·fftfreq(W)`, pero el código
  multiplica por `W`. El docstring del módulo dice "rad/muestra" y contradice su propia
  advertencia de unidades. En `fetch_exp_m3_primary_corpus.py:447-450` se lee "Todo el corpus
  M2 era cuadrado", y es falso (ver H2).
- *Runners monolíticos.* `run_exp_m3.py` tiene 863 LOC. Es aceptable para research, pero no
  sirve como base de producción.

## 3. Auditoría matemática

### 3.1 Formalización

Offline (lo que posee el repo):

```text
Normal tangent-space:  N = normalize(−sx·p, −sy·q, 1)
Inversa:               p = −nx/(sx·nz),  q = −ny/(sy·nz)            (PR-MATH-A)
Poisson periódico:     ĥ(ω) = (−i·ωx·p̂ − i·ωy·q̂)/(ωx² + ωy²),  ĥ(0)=0, Nyquist→0
Sensibilidad (σ iid):  E[δp²] ≈ σ²·(nz² + nx²)/nz⁴  ⇒  |δp| ~ σ/nz²   (hiperbólica)
Tikhonov:              g*(nz) = nz/(nz² + λ²),  max |g*| = 1/(2λ) en nz = λ
```

Comprobé las derivaciones a mano: `dJ/dg = 0` da `g* = nz/(nz²+λ²)`, y
`d/dnz[nz/(nz²+λ²)] = (λ²−nz²)/(…)²` se anula en `nz = λ`. Coinciden con
`nz_policies.py` `[LEÍDO]`.

Runtime (CS/ENB, fuera del repo): `Δuv = −(V.xy/V.z)·s·(1−h)`. Es lineal en la profundidad
`(1−h)` y recíproco en `V.z`. Las mitigaciones estándar son acotar `V.z`, el *offset limiting*
y más capas en ángulo rasante (ver §5, S6).

### 3.2 ¿Lineal o recíproca?

- **Altura ↔ gradiente:** es lineal (un operador lineal invariante a traslación, diagonal en
  Fourier). Por eso la escala global y el signo son ambigüedades exactas y el oráculo afín
  `[LEÍDO]` es el instrumento correcto para evaluar.
- **Gradiente ↔ normal:** es recíproca en `nz`. Es la única fuente real de inestabilidad, y el
  repo la trata bien con las políticas RAW/FLOOR/TIKHONOV y midiendo `floor_hits`.
- **Consecuencia no modelada (H3):** como la relación es recíproca, el error de cuantización
  Q8 depende fuertemente de **qué escala de pendiente** se le asigna al height. Esa escala es
  una elección implícita del código (`c = 1`: altura [0,1] por unidad UV).

### 3.3 Hallazgos

| # | Hallazgo | Severidad | Evidencia |
|---|---|---|---|
| **H1** | `OracleOnly.normal_height_residual_oracle` busca la strength en una rejilla **lineal** ±`linspace(0.05, 5, 25)` (paso 0.206). En pares **perfectamente coherentes** reporta 9–15° de desacuerdo cuando la strength real cae entre puntos o bajo 0.05: s=0.1 → 13.44°, s=0.02 → 9.35°, s=0.005 → 14.27°, s=0.001 → 15.57°. M2 documenta que la escala authored varía entre 2e-4 y 2.5e+3, siete órdenes de magnitud. | **Crítica (validez)** | `[EJECUTADO]`. Afecta la mediana de 13.40° del diagnóstico §5 de M4 (numéricamente indistinguible del artefacto) y la pertenencia a la Cohort B de M3 (filtros 20/30/40°). **No** afecta C1/C2 de M4, que usan un fit afín cerrado. |
| **H2** | El solver fija una convención de aspecto **sin declararla**: `x = j/W`, `y = i/H`, es decir que **el tile es un cuadrado físico** (equivale a un texel de aspecto `H/W`). Un normal map no dice qué convención usó su autor: si pintó la pendiente por texel cuadrado, v1 reconstruye con anisotropía; si la verdad es «tile cuadrado», la corrección «texel cuadrado» sesga igual. El defecto es el parámetro oculto, no una de las dos convenciones. Medido con normal float sin ruido, la convención equivocada da corr 0.966–0.996 y RMSE alineado de 9–26 % de σ según el aspecto (en 2:1, 0.966 y 26 % con verdad «texel cuadrado» resuelta por v1, y 0.975 y 22 % en el sentido inverso); la acertada da 1.0000 (error ≤ 1e-15). La matriz completa está en §6. El corpus M2 contiene 3/34 assets no cuadrados (Concrete035 2048×1024, WoodFloor043 2048×1024, PavingStones054 1365×2048), aplastados además a 512² por `load_asset`. | **Alta** para cualquier entrada no cuadrada; media (histórica) | `[EJECUTADO]` + manifest `[LEÍDO]`. Solo el fetch del corpus primario de M3 rechaza no cuadradas (`fetch_exp_m3_primary_corpus.py:447-452`); `load_cohort_a` y `load_asset` no lo verifican. M4/M5 quedan fuera de H2 porque ese corpus es cuadrado por construcción, no porque el loader lo compruebe. El test `h_rect` de M4 usa un campo nulo y no ejercita la anisotropía. Qué convención usó el autor de cada asset M2 no consta en el repo `[NO VERIFICADO]`. |
| **H3** | El techo **SELF-Q8** de M4 (`self_forward`) depende de una escala de pendiente arbitraria. Mismo height, Q8, distintas intensidades `c`: S07 va de 7e-5 a 3.6e-2 de RMSE (≈500×) y S09 de 4.5e-4 a 1.7e-2 (no monótono). La conclusión de M4 "cuantización Q8 descartada (+0.0037)" vale solo para `c = 1`, que es mucho más empinada que un normal authored típico. | Alta (validez) | `[EJECUTADO]` sintético. Impacto sobre las medianas reales de M4: `[NO VERIFICADO]` (el corpus no está en el repo). |
| **H4** | `resize_normal` re-cuantiza a uint8 con `astype(np.uint8)`, que **trunca**, y vuelve a cuantizar en el resize de Pillow modo L. Es el hermano que el fix #653 (`resize_height` → float) dejó intacto. Produce un sesgo de −0.5 LSB: la normal (0,0,1) sale con nx = −0.0039. En los sintéticos periódicos 1024→512 con pendiente suave (c = 0.05 y 0.01), el RMSE del camino AUTH es 5.3× y 21.5× mayor que con resize float en S07, 1.7× y 10.2× en S15, y no cambia en S09 (0.9–1.0×); el máximo absoluto, 0.023 (S07, c = 0.01), es del orden de `T_DELTA_RMSE = 0.02`. Solo el camino AUTH pasa por acá (SELF se arma desde el height float), así que **infla DELTA de forma asimétrica a 512**. | Alta (validez) | `[EJECUTADO]` sobre sintéticos de pendiente muy suave; el corpus real no se usó en esta medición. M4 atribuye ~0.009 de delta al resize. Que H4 sea parte de ese número es plausible pero `[NO VERIFICADO]`. |
| **H5** | `fd_forward` (control FD de M4) deriva por **muestra** y el solver por **unidad UV**, así que las normales FD son W× más planas. En Q8 el RMSE resulta 8× peor (0.00387 frente a 0.00047 con FD en unidades UV), y en float las dos versiones son idénticas (0.00041). El control FD-Q8 mide un cambio de régimen de cuantización, no la discretización FD. | Media | `[EJECUTADO]`. La conclusión "FD tolerable" sigue en pie, pero por otra razón. |
| **H6** | No conmutatividad entre resize y forward: en contenido con creases (S09), el normal reescalado no es igual al normal del height reescalado, y deja RMSE ≈ 0.04 aun con resize float. A 512 el par AUTH tiene un piso estructural que SELF no tiene. | Media | `[EJECUTADO]`. El secundario nativo 1024 de M4 lo controla, así que la mitigación ya existe. |
| **H7** | Costo: `integrate_periodic` v1 (complex128) tarda 0.85–1.9 s en 2048² y 8.7–9.0 s en 4096², en 4 cores. El diseño A §J estimaba "<1 s/2K total". Con `rfft2` la misma solución en cuadradas tarda 0.41 s en 2K y 0.88 s en 4K, con diferencia ≤ 1e-17. | Media | `[EJECUTADO]` (contenedor de 4 cores; el tiempo varía entre corridas). |
| **H8** | El paquete raíz hace imports eager (ver §2). | Baja-media | `[EJECUTADO]` |

**Patrón dominante.** H2/H5, H4 y la asimetría 16-bit son instancias de la regla de
`AGENTS.md`: *arreglar un hermano y no al otro*. NP-M0 fijó unidades en `freq_axes` y no en
`fd_forward`/`rho_grid`. #653 arregló `resize_height` y no `resize_normal`. El decoder de
height acepta 16-bit y `decode_normal_image` hace `convert("RGB")`; que haya normales PNG de
16 bits en el corpus M3 está `[NO VERIFICADO]`.

### 3.4 Precisión y determinismo

- float64 de punta a punta: no hay drift acumulativo porque no hay integración temporal.
- El determinismo bit a bit está probado dentro de un mismo proceso. Entre plataformas y
  backends FFT está `[NO VERIFICADO]`; el manifest ya registra `fft_backend`.
- **Banding de salida** (exporter futuro): 8 bits son 256 niveles, sumados a la
  interpolación BC4. Recomiendo un dither determinista (blue-noise con seed fija) antes de
  cuantizar, validado en EXP-010 `[INFERENCIA]`.

## 4. Matriz comparativa y veredicto

| Criterio | A — determinista | B — híbrido |
|---|---|---|
| CPU | v1 ~1.9 s/2K, ~9 s/4K; v2 rFFT 0.41 s/2K, 0.88 s/4K `[EJECUTADO]` | Núcleo A + DA3 por tiles, 2–5 min/4K en CPU según el doc B `[NO VERIFICADO]` |
| GPU/VRAM | 0 | 2–6 GB según el doc B `[NO VERIFICADO]` |
| Escalabilidad (n texturas × resolución × contratos) | O(N log N) por textura, paralelismo por proceso, cache por hash; un `HeightField` alimenta N exporters | La propia doc B limita la IA al subconjunto REVIEW |
| Flexibilidad para autores | Parámetros explícitos (strength, signo, percentiles), HITL, overrides reproducibles | Explicaciones en texto y presets por familia; propuestas no reproducibles |
| Fidelidad HF | Exacta (M0, ~1e-16 sintético) `[LEÍDO]` | Igual (mismo núcleo) |
| Fidelidad LF/signo | DC inobservable; LF periódica recuperable (M0); signo vía HITL | Potencial con modelos de dominio material; EXP-008 no ejecutado |
| Determinismo | Sí (mismo proceso) | No (fp16/GPU) |
| Riesgo de licencias | Ninguno | NC/RAIL en la mitad del menú |
| Madurez | Research M0–M5 con instrumentos a reparar | Cero código |

**Veredicto: A, con la secuencia corregida.** Reparar los instrumentos (H1–H5) → re-correr
sobre el corpus congelado como revalidación versionada → EXP-001b/012 → recién ahí los
experimentos de habilitación de B (EXP-007/008/009). B queda como instrumentación dormida; no
hace falta un híbrido nuevo, la Síntesis C ya lo es.

## 5. Plan de remediación

| Prioridad | Acción | Ancla de test (enumera, no muestrea) |
|---|---|---|
| P0 | **H1**: oráculo de strength en forma cerrada (S2) | Parametrizar `s_true ∈ {2, 0.3, 0.1, 0.02, 0.003, −0.05}` × {float, Q8}: ángulo < 1e-6° en float y < 0.5° en Q8, signo correcto. Enumerar los 4 callers (M2/M3×2/M4) |
| P1 | **H2**: `texel_aspect` explícito en `normal_fft_periodic_v2` (S1), sin editar v1; en no cuadradas sin convención declarada falla cerrado; gate de cuadradas en `load_asset` y `load_cohort_a` (hoy solo lo tiene el fetch del corpus M3); marcar los 3 assets M2 como desvío de protocolo | v2 ≡ v1 (≤1e-12) en cuadradas y, con `texel_aspect = H/W`, en no cuadradas; round-trip exacto por convención en `(64,128)`, `(128,64)`, `(96,160)`, `(63,65)`; matriz verdad × solver 2×2 (diagonal exacta, fuera de la diagonal sesgada); `None` en no cuadrada → `ValueError` |
| P1 | **H4**: `resize_normal` en float (S3) | Normal plana → `abs(mean(nx)) < 1e-7`; RMSE AUTH ≤ float + ε |
| P1 | **H3**: SELF a la strength fitada del par (S4) | SELF-Q8 debe ser invariante (±10 %) a `c ∈ {1, 0.1, 0.01}` tras calibrar |
| P1 | **H5**: `fd_forward` en las unidades del solver (S5) | FD-Q8 ≈ spectral-Q8 (≤ 2×) en contenido band-limited |
| P2 | Ancla AST de unidades: el conjunto de módulos que llaman `np.fft.fftfreq`/`rfftfreq` o derivan con `np.roll` se congela por igualdad literal `{módulo: unidad_declarada}` | Un módulo nuevo con su propia convención rompe el test |
| P2 | **H7**: rFFT en el camino productivo (incluido en S1) | Benchmark registrado en el manifest |
| P2 | **H8**: `__getattr__` lazy (PEP 562) en `sky_claw/__init__.py` | Toca el API raíz: requiere aprobación (alto impacto) |
| P3 | Corregir el drift documental (docstrings de `freq_axes` y el comentario M3) | — |

Las corridas históricas (M2–M5) **no se reescriben**: van a una revalidación versionada, con
el mismo criterio que `m2-m3-math-revalidation-protocol.md`.

### S1 — `normal_fft_periodic_v2` (`texel_aspect` explícito + rFFT) `[EJECUTADO: el bloque se extrajo de este archivo y se corrió, ver §6]`

La convención de aspecto es un **parámetro de entrada**, no una propiedad que se pueda inferir del
normal map. Hay dos convenciones legítimas y ninguna es universal: `1.0` (texel cuadrado, la pendiente
que pinta el autor es por texel) y `n_rows / n_cols` (tile físicamente cuadrado, `x = j/W`,
`y = i/H`: es lo que hace v1 sin decirlo).

```python
ALGORITHM_ID = "normal_fft_periodic_v2"
_NYQ_ATOL = 1e-12


def resolve_texel_aspect(n_rows: int, n_cols: int, texel_aspect: float | None) -> float:
    """Convención de aspecto, siempre explícita: ancho físico del texel / alto físico del texel.

    ``1.0``: texel cuadrado. ``n_rows / n_cols``: tile físicamente cuadrado (la convención de v1).
    ``None`` solo vale en grillas cuadradas, donde las dos coinciden; en no cuadradas falla cerrado
    en vez de elegir una por quien llama.
    """
    if n_rows < 2 or n_cols < 2:
        raise ValueError(f"grid demasiado chico: {(n_rows, n_cols)}")
    if texel_aspect is None:
        if n_rows != n_cols:
            raise ValueError(
                f"grilla no cuadrada {(n_rows, n_cols)}: declarar texel_aspect "
                "(1.0 = texel cuadrado; n_rows/n_cols = tile cuadrado, la convención de v1)"
            )
        return 1.0
    if not (np.isfinite(texel_aspect) and texel_aspect > 0.0):
        raise ValueError(f"texel_aspect debe ser finito y > 0: {texel_aspect!r}")
    return float(texel_aspect)


def freq_axes_v2(
    n_rows: int, n_cols: int, texel_aspect: float | None = None
) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
    """``(wy (H,1), wx (1, W//2+1))`` en rad por unidad de ALTO de tile (``y = i/H``).

    ``wy = 2π·H·fy`` y ``wx = 2π·H·fx / texel_aspect``. Con ``texel_aspect = H/W`` reproduce v1
    (``x = j/W``); en cuadradas v1 y v2 coinciden numéricamente (≤ 1e-12, no bit a bit: v1 usa
    ``fft2`` complejo y v2 ``rfft2``).
    """
    aspect = resolve_texel_aspect(n_rows, n_cols, texel_aspect)
    fy = np.fft.fftfreq(n_rows)
    fx = np.fft.rfftfreq(n_cols)
    wy = 2.0 * np.pi * n_rows * fy
    wx = 2.0 * np.pi * n_rows * fx / aspect
    wy[np.abs(np.abs(fy) - 0.5) < _NYQ_ATOL] = 0.0  # derivada nula en Nyquist (convención v1)
    wx[np.abs(fx - 0.5) < _NYQ_ATOL] = 0.0
    return wy.reshape(n_rows, 1), wx.reshape(1, fx.size)


def spectral_gradients_v2(
    h: NDArray[np.float64], texel_aspect: float | None = None
) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
    """Forward coherente con ``integrate_periodic_v2`` (mismas unidades y misma convención)."""
    n_rows, n_cols = h.shape
    wy, wx = freq_axes_v2(n_rows, n_cols, texel_aspect)
    h_hat = np.fft.rfft2(h)
    shape = (n_rows, n_cols)
    return np.fft.irfft2(1j * wx * h_hat, s=shape), np.fft.irfft2(1j * wy * h_hat, s=shape)


def integrate_periodic_v2(
    p: NDArray[np.float64], q: NDArray[np.float64], texel_aspect: float | None = None
) -> NDArray[np.float64]:
    """Poisson periódico (Frankot–Chellappa) → ``h`` de media cero, float64."""
    if p.ndim != 2 or p.shape != q.shape:
        raise ValueError(f"p y q deben ser 2D y del mismo shape: {p.shape} vs {q.shape}")
    if not (np.all(np.isfinite(p)) and np.all(np.isfinite(q))):
        raise ValueError("p/q no finitos: fail-closed antes de contaminar el espectro")
    n_rows, n_cols = p.shape
    wy, wx = freq_axes_v2(n_rows, n_cols, texel_aspect)
    denom = wx * wx + wy * wy
    nulo = denom == 0.0  # DC + Nyquist autoparejados: el numerador también es 0
    denom[nulo] = 1.0
    h_hat = (-1j * wx * np.fft.rfft2(p) - 1j * wy * np.fft.rfft2(q)) / denom
    h_hat[nulo] = 0.0
    return np.asarray(np.fft.irfft2(h_hat, s=(n_rows, n_cols)), dtype=np.float64)
```

Nota: `irfft2` impone la simetría hermitiana por construcción. El diagnóstico
`max_imag_residual` de v1 deja de tener sentido y no se reemplaza por un número inventado.

### S2 — Oráculo de strength en forma cerrada `[EJECUTADO]`

```python
GradFn = Callable[[NDArray[np.float64]], tuple[NDArray[np.float64], NDArray[np.float64]]]


def _median_angle_deg(normal: NDArray[np.float64], h: NDArray[np.float64], s: float, grad: GradFn) -> float:
    p, q = grad(h * s)
    inv = 1.0 / np.sqrt(p * p + q * q + 1.0)
    dot = (-p * normal[..., 0] - q * normal[..., 1] + normal[..., 2]) * inv
    return float(np.median(np.degrees(np.arccos(np.clip(dot, -1.0, 1.0)))))


def oracle_strength(
    normal: NDArray[np.float64],
    h_authored: NDArray[np.float64],
    grad: GradFn,
    *,
    nz_floor: float = 1e-3,
    refine_octaves: float = 2.0,
    refine_steps: int = 41,
) -> dict[str, float]:
    """Strength global por LS en espacio de gradiente (pesos nz²) + refinamiento log acotado.

    ``s0 = <w·g_auth, g_h> / <w·g_h, g_h>`` es cerrado, conserva el signo y no tiene límites
    de rango. El refinamiento (±2 octavas) optimiza la métrica final (mediana angular).
    """
    hc = h_authored - h_authored.mean()
    nz = np.maximum(normal[..., 2], nz_floor)
    pa, qa = -normal[..., 0] / nz, -normal[..., 1] / nz
    ph, qh = grad(hc)
    w = np.clip(normal[..., 2], 0.0, 1.0) ** 2
    den = float(np.sum(w * (ph * ph + qh * qh)))
    s0 = float(np.sum(w * (pa * ph + qa * qh)) / den) if den > 0.0 else 0.0
    if s0 == 0.0:
        return {"height_normal_oracle_agreement_deg": float("nan"), "oracle_best_strength": 0.0}
    grid = s0 * np.exp2(np.linspace(-refine_octaves, refine_octaves, refine_steps))
    angs = [_median_angle_deg(normal, hc, float(s), grad) for s in grid]
    k = int(np.argmin(angs))
    return {"height_normal_oracle_agreement_deg": angs[k], "oracle_best_strength": float(grid[k])}
```

Resultado medido (S09, 256²): para `s ∈ {2, 0.3, 0.1, 0.02, 0.003, −0.05}` el ángulo es
0.0000° en float y 0.17–0.18° en Q8, con signo y strength recuperados. El oráculo actual da
hasta 15.6° en los mismos pares.

### S3 — `resize_normal` sin re-cuantizar (hermano de #653) `[EJECUTADO]`

```python
def resize_normal(n: NDArray[np.float64], size: int) -> NDArray[np.float64]:
    if n.shape[0] == size and n.shape[1] == size:
        return np.asarray(n, dtype=np.float64)
    channels = [
        np.asarray(
            Image.fromarray(np.asarray(np.clip(n[..., c], -1.0, 1.0), dtype=np.float32)).resize(
                (size, size), Image.Resampling.BILINEAR
            ),
            dtype=np.float64,
        )
        for c in range(3)
    ]
    out = np.stack(channels, axis=-1)
    return np.asarray(out / np.maximum(np.linalg.norm(out, axis=-1, keepdims=True), 1e-12), dtype=np.float64)
```

### S4 — SELF calibrado al régimen de pendiente del par (H3) `[NO EJECUTADO sobre corpus]`

```python
s_star = oracle_strength(auth_normal, height, spectral_gradients)["oracle_best_strength"]
n_self = self_forward(height * s_star, bits=8)  # SELF y AUTH comparten régimen nz
```

### S5 — `fd_forward` en las unidades del solver (H5) `[EJECUTADO como variante, ver §6]`

```python
def fd_forward_v2(
    h: NDArray[np.float64], texel_aspect: float | None = None
) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
    """Diferencias centradas por UNIDAD de alto de tile: misma unidad y convención que ``freq_axes_v2``."""
    n_rows, n_cols = h.shape
    aspect = resolve_texel_aspect(n_rows, n_cols, texel_aspect)
    px = (np.roll(h, -1, axis=1) - np.roll(h, 1, axis=1)) * (0.5 * n_rows / aspect)
    qy = (np.roll(h, -1, axis=0) - np.roll(h, 1, axis=0)) * (0.5 * n_rows)
    return px, qy
```

Medido sobre un campo band-limited (≤ 12 ciclos por tile) en 128², 128×256 y 256×128: con estas
unidades el FD difiere del derivado espectral en un 2–3 % (error relativo RMS); por muestra, sin la
conversión, el desajuste es del 99 %. Es la misma clase de defecto que H5, ahora también por eje.

### S6 — Referencia POM para el preview offline NP-V0 (GLSL 3.30) `[NO EJECUTADO]`

El repo no tiene rig GL. Esto es una referencia de la matemática recíproca en `V.z`, no
código verificado.

```glsl
#define MAX_LAYERS 64
uniform sampler2D heightTex;  // r ∈ [0,1]; 1 = superficie, 0 = profundidad máxima
uniform float heightScale;    // profundidad máxima en unidades UV
uniform float minLayers;      // p.ej. 8
uniform float maxLayers;      // p.ej. 32 (≤ MAX_LAYERS)

vec2 parallaxUV(vec2 uv, vec3 viewTS) {          // viewTS: superficie → ojo, tangent space
    vec3 V = normalize(viewTS);
    float n = mix(maxLayers, minLayers, clamp(V.z, 0.0, 1.0)); // más capas en rasante
    float layer = 1.0 / n;
    vec2 dUV = (V.xy / max(V.z, 0.15)) * heightScale * layer;  // acota la hipérbola 1/V.z
    vec2 gx = dFdx(uv), gy = dFdy(uv);                         // derivadas FUERA del loop
    vec2 cur = uv;
    float curLayer = 0.0;
    float curDepth = 1.0 - textureGrad(heightTex, cur, gx, gy).r;
    for (int i = 0; i < MAX_LAYERS && curLayer < curDepth; ++i) {
        cur -= dUV;
        curDepth = 1.0 - textureGrad(heightTex, cur, gx, gy).r;
        curLayer += layer;
    }
    vec2 prev = cur + dUV;                                     // refinamiento por secante
    float after = curDepth - curLayer;
    float before = (1.0 - textureGrad(heightTex, prev, gx, gy).r) - (curLayer - layer);
    float w = after / min(after - before, -1e-6);
    return mix(cur, prev, clamp(w, 0.0, 1.0));
}
```

El piso `0.15` de `V.z` (≈81°) es un parámetro de diseño. Hay que calibrarlo contra EXP-011/012.

## 6. Validación

Tests existentes (sin cambios): `.venv/bin/python -m pytest tests/test_native_parallax_*.py tests/test_parallaxr_assisted.py`
→ **371 passed** `[EJECUTADO]`.

Reproducción mínima de H1 (sin corpus):

```python
h = PERIODIC_CASES["S09_bricks"](512, 512); h = (h - h.min()) / (h.max() - h.min())
p, q = spectral_gradients((h - h.mean()) * 0.1)
OracleOnly.normal_height_residual_oracle(normals_from_gradients(p, q), h)
# → agreement ≈ 13.44°, best_strength = 0.05 (par perfectamente coherente)
```

Reproducción mínima de H2, en las dos direcciones. Corr y RMSE alineado no dependen de la escala
global, así que la normal se arma con pendientes ÷ `R`. Usa `spectral_gradients_v2` e
`integrate_periodic_v2` de S1, y `best_affine`, `pearson`, `normals_from_gradients` y
`gradients_from_normal` del paquete research:

```python
R, C = 128, 256
rng = np.random.default_rng(0)
i, j = np.mgrid[0:R, 0:C].astype(float)
h = np.zeros((R, C))
for _ in range(40):
    kx = int(rng.integers(-12, 13))
    ky = int(rng.integers(-12, 13))
    if kx == 0 and ky == 0:
        continue
    h += rng.uniform(0.2, 1) / np.hypot(kx, ky) * np.sin(2 * np.pi * (kx * j / C + ky * i / R) + rng.uniform(0, 6.28))
h *= 3.0
for verdad in (1.0, R / C):  # texel cuadrado | tile cuadrado (la convención de v1)
    p, q = spectral_gradients_v2(h, verdad)  # la pendiente «verdadera» bajo esa convención
    pp, qq, _ = gradients_from_normal(normals_from_gradients(p / R, q / R))
    for solver in (1.0, R / C):
        rec = integrate_periodic_v2(pp, qq, solver)
        print(f"verdad={verdad:.2f} solver={solver:.2f} corr={pearson(rec, h):.4f} "
              f"rmse/sigma={best_affine(rec, h)['rmse'] / h.std():.4f}")
```

Resultado `[EJECUTADO: el bloque se extrajo de este archivo y se corrió]`. `integrate_periodic` de v1 da
lo mismo que `solver = H/W` (diferencia de corr < 1e-9 en todos los casos):

| Forma | Verdad | Solver `aspect = 1.0` | Solver `aspect = H/W` (= v1) |
|---|---|---|---|
| 128×256 (2:1) | texel cuadrado (`1.0`) | corr 1.0000, rmse/σ 0.0000 | corr 0.9657, rmse/σ 0.2598 |
| 128×256 (2:1) | tile cuadrado (`H/W`) | corr 0.9750, rmse/σ 0.2223 | corr 1.0000, rmse/σ 0.0000 |
| 256×128 (1:2) | texel cuadrado (`1.0`) | corr 1.0000, rmse/σ 0.0000 | corr 0.9827, rmse/σ 0.1854 |
| 256×128 (1:2) | tile cuadrado (`H/W`) | corr 0.9670, rmse/σ 0.2547 | corr 1.0000, rmse/σ 0.0000 |
| 192×256 (4:3) | texel cuadrado (`1.0`) | corr 1.0000, rmse/σ 0.0000 | corr 0.9948, rmse/σ 0.1023 |
| 192×256 (4:3) | tile cuadrado (`H/W`) | corr 0.9957, rmse/σ 0.0927 | corr 1.0000, rmse/σ 0.0000 |

La diagonal es exacta (error ≤ 1e-15) y fuera de ella el sesgo corre en ambos sentidos. Por eso la
corrección es declarar la convención, no cambiar la de v1 por la otra. Las otras formas del cuadro salen
de cambiar `R, C` en el mismo bloque. En la misma corrida, sobre los bloques S1 y S5 extraídos de este
archivo: equivalencia con v1 ≤ 1e-12 en cuadradas (32², 64², 512²) y, con `texel_aspect = H/W`, en
`(64,128)`, `(128,64)`, `(96,160)` y `(63,65)`; round-trip exacto (error < 1e-9) con `texel_aspect` en
`{1.0, H/W, 0.5, 2.0}` sobre esas cuatro formas; y `ValueError` con `None` en no cuadradas o con un
aspecto no positivo o no finito.

## 7. No verificado / riesgos residuales

- Impacto de H3/H4 sobre las medianas reales de M4/M5: requiere el corpus (fuera del repo).
- Qué convención de aspecto usó el autor de cada normal map no cuadrado del corpus M2: no se infiere
  del archivo y no consta en el repo (H2).
- Profundidad de bits de los PNG normales del corpus M3, que pueden truncarse en `convert("RGB")`.
- Determinismo FFT entre plataformas.
- Semántica exacta de altura en `_p`/CM por stack (CS vs ENB): `REAL_RIG_REQUIRED`.
- Los costos del enfoque B citados vienen del documento de diseño, no se midieron.

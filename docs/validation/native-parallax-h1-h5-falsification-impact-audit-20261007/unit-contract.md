# H2 — Contrato de unidades del gradiente normal

> **Base:** `97dcc7ab9ade29153faa0ccec428b78612cfc04a` · **Carácter:** AUDIT_ONLY, READ-ONLY
> **No se implementó `normal_fft_periodic_v2`. No se editó código científico.**

## 1. Respuesta

```
H2_NUMERICAL_OBSERVATION = REPRODUCED
H2_BUG_CLASSIFICATION    = NOT_PROVEN
H2_UNIT_CONTRACT         = UV_NORMALIZED
```

## 2. Reconstrucción del contrato desde el repositorio

El contrato **implementado** no está ambiguo: está documentado, implementado y **congelado por
test**.

| Fuente | Evidencia |
|---|---|
| `normal_fft_periodic.py` (docstring del módulo) | «`p = ∂h/∂x`, `q = ∂h/∂y` con `x = j/W ∈ [0,1)` e `y = i/H` (**coordenadas de textura, NO índices de muestra**)» |
| `normal_fft_periodic.py` (`freq_axes`) | `wx = 2π·fftfreq(W)·W = 2π·k` «[rad por UNIDAD de x, eje de columnas]»; `wy = 2π·l` |
| `normal_fft_periodic.py` (ADVERTENCIA) | «`2π·fftfreq` a secas son radianes **POR MUESTRA** y derivarían respecto del índice (p escalado por 1/W, resolution-dependiente). La convención elegida hace a (p, q) independientes de la resolución» — y llama a lo otro *«bug real cazado por NP-M0»* |
| `tests/test_native_parallax_math_spike.py::test_frecuencias_en_espacio_de_textura` | congela `wx[0,1] == 2π` y `wy[1,0] == 2π` sobre la grilla **NO cuadrada** `(8, 16)` |
| `docs/design/research/native-parallax/np-m0-results.md` (§5, líneas 322-324) | «el spike cazó un bug propio de convención (`2π·fftfreq` = rad/muestra vs rad/unidad-de-textura)» |
| `normal_from_height.spectral_gradients` | usa `freq_axes` → misma convención (la usan `self_forward`, el oráculo y M4/M5) |
| `normal_fft_periodic.integrate_periodic` | usa `freq_axes` → misma convención |

**`NORMAL_GRADIENT_UNIT_CONTRACT = UV_NORMALIZED`** — es decir `x = j/W`, `y = i/H`: el tile
mapea al cuadrado unitario en UV y `(p, q)` son pendientes **por unidad de UV** (rad/unidad de
textura), independientes de la resolución del grid.

El test es decisivo: corre sobre una grilla `(8, 16)`. Bajo `ISOTROPIC_TEXEL` (`S = max = 16`)
la componente `wy[1,0]` valdría `4π`, no `2π`, y el test **fallaría**. La suite verde (371 tests)
es, por lo tanto, evidencia de que el contrato es `UV_NORMALIZED`.

## 3. Dónde difieren los dos contratos

| Contrato | Coordenada física | `p` | `q` |
|---|---|---|---|
| **UV_NORMALIZED** (implementado) | `x=j/W`, `y=i/H` | `W·∂h/∂j` | `H·∂h/∂i` |
| **ISOTROPIC_TEXEL** (propuesto por el audit) | `x=j/S`, `y=i/S`, `S=max(H,W)` | `S·∂h/∂j` | `S·∂h/∂i` |

- Si `W = H` → `S = W = H` → **idénticos** (verificado: `abs_diff = 0.0` bit a bit).
- Si `W ≠ H` → difieren en un factor `S/W` en `p` y `S/H` en `q`.

## 4. Observación numérica — reproducida, y CIRCULAR

`scripts/phase_b_units.py` construye un sintético **periódico en el grid** y lo evalúa bajo
**ambos** contratos, con la normal construida según cada uno. Resultado (recortado):

| grid | sintético construido bajo | `v1` corr | `v1` ángulo | `v2` corr | `v2` ángulo |
|---|---|---|---|---|---|
| 64×128 | `texel_isotropic` | 0.9547 | 18.81° | **1.0000** | 0.00° |
| 64×128 | `uv_normalized` | **1.0000** | 0.00° | 0.9547 | 18.81° |
| 96×160 | `texel_isotropic` | 0.9752 | 13.90° | **1.0000** | 0.00° |
| 96×160 | `uv_normalized` | **1.0000** | 0.00° | 0.9752 | 13.90° |
| 63×65 | ambos | ≈1.0000 | ≤0.78° | ≈1.0000 | ≤0.78° |
| 128×128 | ambos | 1.0000 | 0.00° | 1.0000 | 0.00° |

La observación del audit se reproduce (con sintético construido bajo su contrato, `v1` falla y
`v2` acierta). **Pero la simetría es exacta**: con el mismo grid construido bajo el contrato
opuesto, el resultado se **invierte** — `v1` acierta y `v2` falla.

> Un sintético rectangular sólo prueba que un contrato reconstruye mejor el sintético
> construido bajo ESE contrato. No es prueba de cuál contrato es el correcto.

Eso es exactamente la circularidad que el brief ordena no usar como prueba. Por eso
`H2_BUG_CLASSIFICATION = NOT_PROVEN`: el repo **no declara en ningún manifest, test o doc** que
los normal maps del corpus codifiquen pendiente por texel isótropo. Esa premisa es externa al
repositorio y no está verificada aquí.

## 5. Impacto real: NINGUNO sobre M3/M4/M5

| Superficie | Resolución nativa | ¿Cuadrada? | ¿Alcanza el solver con `W≠H`? |
|---|---|---|---|
| **M3 Cohort A** (31 assets, corpus primario) | 31/31 a 1024×1024 | **Sí, 100%** | No |
| **M2 manifest** (34 assets, insumo de Cohort B) | 31×2048², 2×2048×1024, 1×1365×2048 | No (3/34) | No: `load_asset` reescala a 512² **antes** del solver |

Dos consecuencias:

1. El corpus primario (M3 Cohort A, el que produce las decisiones de M4/M5) es **100 % cuadrado**,
   así que `v1 ≡ v2` y H2 es **inobservable** allí.
2. El audit externo afirma «M3+ rechaza no cuadradas». Eso es **impreciso**: `load_cohort_a` no
   tiene gate de cuadradas. La conclusión (M4/M5 no afectados) es correcta, pero por
   **composición del corpus + reescalado a cuadrado en `load_asset`**, no por un rechazo.
   El único gate de cuadradas real está en `run_exp_m4.run_asset_native` (`if native_resolution[0]
   != native_resolution[1]: return None`), que es la secundaria §4.1 y no la decisión primaria.

## 6. Inconsistencia interna detectada (deuda, no defecto activo)

`fd_forward` (`solver_coherence.py:95-96`) deriva por **muestra** (`(roll diff)/2`), no por unidad
UV — es una **tercera** convención conviviendo con `freq_axes` (rad/unidad UV) y con
`frequency_coherence.rho_grid` (ciclos/tile). Ver H5 en `decision-impact.md`: en el régimen
float el factor constante es absorbido exactamente por el fit afín global, así que no hay efecto
numérico en la decisión; en Q8 la interacción unidad↔cuantización sí dispersa el resultado
(ratio 0.063–5.94).

## 7. Adjudicación

```
H2_NUMERICAL_OBSERVATION = REPRODUCED
H2_BUG_CLASSIFICATION    = NOT_PROVEN
H2_UNIT_CONTRACT         = UV_NORMALIZED
H2_IMPACT_ON_M4_M5       = NONE
```

**No corresponde code fix** en este slice: (a) el contrato implementado es el declarado y
congelado; (b) el defecto no es demostrable sin una premisa externa no verificada; (c) el corpus
primario es cuadrado, así que no hay efecto que corregir para las decisiones vigentes. Si en el
futuro entra al corpus primario algún asset con `W ≠ H`, el gate de cuadradas debería ser
**explícito y fail-closed** en el loader, no implícito en la composición del manifest.

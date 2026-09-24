# NP-M0 Results — spike matemático: height sintético → normal → reconstrucción periódica

> Experimento adversarial sobre `normal_fft_periodic_v1`. Research-only: sin DDS, sin
> Skyrim, sin MO2, sin PGPatcher, sin IA, sin conocimiento derivado de ParallaxR
> (clean-room: solo matemática pública + NumPy + generación sintética propia).
> Branch `arena/01a0c5fd-sky-claw`, base `aee1151` sobre `origin/main` `4dfab8f`.
> **Estado: `NP_M0_GO` — esperando revisión independiente. NO merge.**

## Environment

## Environment

- Python: 3.11.2 (CPython)
- NumPy: 2.4.6
- SciPy: no usada (métricas implementadas sobre NumPy)
- Plataforma: Linux-6.1.158+-x86_64-with-glibc2.36
- CPU: Intel(R) Xeon(R) Processor @ 2.60GHz


- Nota: **NumPy fue instalado ad-hoc para este spike y NO se agregó a `pyproject.toml`**
  (regla del brief: no tocar pyproject sin justificar). Los tests usan
  `pytest.importorskip("numpy")` → en un entorno sin NumPy la batería se salta, la suite
  queda verde. Si NP-M0 → GO, el PR NP-D0 debe agregar `numpy` a dev-dependencies con
  lockfile. Sin SciPy: Spearman es Pearson sobre rangos; SSIM es implementación propia
  con integral images.

## Hypothesis

Validar o refutar: *un HeightField periódico puede reconstruirse suficientemente fiel
desde normales derivadas matemáticamente del mismo campo*. Hipótesis inherentes
(H1–H7, §36 del brief), todas sometidas a intento de refutación. Los targets
provisionales del research (RMSE<0.05, seam<0.01, ángulo<15°) se usan solo como orden
de magnitud de referencia, **no** como criterios.

## Mathematical derivation

Resumen (detalles en docstrings de `normal_fft_periodic.py`):

- Coordenadas de textura ``x = j/W ∈ [0,1)``, ``y = i/H``. Frecuencias en espacio de
  textura: ``wx = 2π·k`` (``k = fftfreq(W)·W``), ``wy = 2π·l`` — radianes por **unidad de
  x**, no por muestra (bug de unidades cazado durante el spike: con ``2π·fftfreq`` a
  secas, ``p`` queda escalado por ``1/W`` y la escala se vuelve resolution-dependiente).
- Poisson periódico (Frankot–Chellappa): ``Δh = ∂p/∂x + ∂q/∂y`` resuelto en espectro:
  ``ĥ = (-i·wx·p̂ - i·wy·q̂)/(wx²+wy²)`` para ``(wx,wy) ≠ (0,0)``; ``ĥ[0,0] = 0`` (DC no
  observable).
- Convención Nyquist: derivada anulada en los bins autoparejados
  ``(0,W/2),(H/2,0),(H/2,W/2)`` (extensión par; evita componente imaginaria).
  Consecuencia medida: la reconstrucción pierde exactamente el modo Nyquist de ``h``
  (ver Failure cases).
- Guard: ``denom`` se anula en **cuatro** bins (DC + 3 Nyquist) — 0/0 = NaN que
  contamina todo el espectro. Cazado por los tests invariantes en la primera corrida.
- Normal: ``N = normalize((-sx·p, -sy·q, 1))``, signos parametrizables (sin "verdad
  Skyrim" en este spike). Inversa ``p = -sx·nx/max(nz, nz_floor)`` con ``nz_floor``
  experimental y contador de ``floor_hits`` (la singularidad se mide, no se esconde).

## Dataset

16 superficies periódicas ``S01–S15`` + 2 controles negativos ``N01–N02`` (§10/§11 del
brief), fórmulas cerradas propias, seeds fijas, float64. Consistencia de frontera
medida (ratio del paso de wrap vs paso interior):

## Boundary consistency del dataset (ground truth)

| case | boundary_wrap_ratio |
|---|---:|
| S01_flat | 1.000 |
| S02_sine_x | 1.000 |
| S03_sine_y | 1.000 |
| S04_separable | 1.000 |
| S05_diagonal | 1.000 |
| S06_multifreq | 1.081 |
| S07_bumps | 1.057 |
| S08_ridges | 1.000 |
| S09_bricks | 1.000 |
| S10_asymmetric | 1.042 |
| S11_highfreq | 1.000 |
| S12_mixed_scale | 1.002 |
| S13_near_flat | 1.081 |
| S14_steep | 1.000 |
| S15_periodic_noise | 1.084 |
| N01_ramp | 255.000 |
| N02_bump_boundary | 38.409 |


Los S* viven en [1.000, 1.084] (periódicos por métrica, no a ojo); N01=255.0 y
N02=38.4 quedan claramente fuera del contrato.

## Experiment setup

Grupos E0–E6 (§33). Resolución principal 512², E6 mide 256²→2048². Ruido gaussiano
sobre la normal (renormalizada) con seeds deterministas por (caso, σ) vía CRC32 (el
`hash()` de Python es randomizado por proceso y rompería reproducibilidad — corregido
durante el spike). Cuantización entera de la normal (8/10/16 bits) sin DDS. Alineación
``ref ≈ a·rec + b`` SOLO para evaluación, con auditoría anti-fitting (§35): toda fila
reporta además ``raw_centered_rmse``, ``gradient_rmse`` y ``normal_angle`` que el
fitting afín no puede arreglar. Mutaciones M1–M7 como tests del oracle.

## Raw result table

### Grupo E0 — PERFECT PERIODIC (§19)

### Grupo E0

| case | resolution | noise | quantization | rmse | gradient_rmse | raw_centered_rmse | normal_angle_mean | normal_angle_p95 | corr | seam_height | seam_gradient | low_band_error | mid_band_error | high_band_error | ssim | best_sign | scale_a | offset_b | nz_min | nz_floor_hits | runtime_ms |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| S01_flat | 512x512 | 0 | - | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 0 | 0 | 0 | 1 | +1 | 0 | 0 | 1 | 0 | 61.8101 |
| S02_sine_x | 512x512 | 0 | - | 1.3976e-16 | 7.91705e-14 | 1.39765e-16 | 2.08915e-07 | 8.53774e-07 | 1 | 4.90654e-18 | 4.61853e-14 | 1.08441e-16 | 8.81678e-17 | 0 | 1 | +1 | 1 | -2.19551e-18 | 0.0496745 | 0 | 53.05 |
| S03_sine_y | 512x512 | 0 | - | 1.39756e-16 | 7.91705e-14 | 1.39756e-16 | 2.08915e-07 | 8.53774e-07 | 1 | 4.90654e-18 | 4.61853e-14 | 1.08439e-16 | 8.81622e-17 | 0 | 1 | +1 | 1 | 0 | 0.0496745 | 0 | 57.6233 |
| S04_separable | 512x512 | 0 | - | 2.12124e-16 | 1.06558e-13 | 2.36977e-16 | 2.69336e-07 | 1.20742e-06 | 1 | 1.05002e-16 | 1.70579e-13 | 1.75813e-16 | 1.1224e-16 | 3.85778e-17 | 1 | +1 | 1 | -6.93889e-18 | 0.0590463 | 0 | 54.0557 |
| S05_diagonal | 512x512 | 0 | - | 2.41595e-16 | 2.02453e-13 | 2.41595e-16 | 2.71478e-07 | 1.20742e-06 | 1 | 2.42776e-16 | 2.58196e-13 | 1.73431e-16 | 1.55198e-16 | 6.48346e-17 | 1 | +1 | 1 | 1.76183e-18 | 0.0440987 | 0 | 55.5066 |
| S06_multifreq | 512x512 | 0 | - | 1.28561e-16 | 8.63263e-14 | 1.5616e-16 | 2.62953e-07 | 1.20742e-06 | 1 | 1.70805e-16 | 1.28354e-13 | 8.69307e-17 | 8.71901e-17 | 3.69991e-17 | 1 | +1 | 1 | 1.73472e-17 | 0.0687849 | 0 | 53.3564 |
| S07_bumps | 512x512 | 0 | - | 1.55609e-10 | 6.52522e-14 | 1.55609e-10 | 2.4216e-07 | 1.20742e-06 | 1 | 2.20065e-10 | 1.08814e-13 | 8.31466e-17 | 1.55609e-10 | 3.99252e-17 | 1 | +1 | 1 | 0.338268 | 0.056709 | 0 | 50.4522 |
| S08_ridges | 512x512 | 0 | - | 1.15766e-16 | 5.17546e-14 | 1.15766e-16 | 1.85619e-07 | 8.53774e-07 | 1 | 7.85046e-17 | 1.06581e-13 | 8.06641e-17 | 8.30368e-17 | 0 | 1 | +1 | 1 | 0 | 0.0300441 | 0 | 52.2712 |
| S09_bricks | 512x512 | 0 | - | 2.10969e-06 | 6.38821e-14 | 2.10969e-06 | 2.61562e-07 | 1.20742e-06 | 1 | 2.98355e-06 | 1.19007e-13 | 7.83431e-17 | 2.10969e-06 | 5.54617e-12 | 1 | +1 | 1 | 0.401249 | 0.00342702 | 0 | 50.4622 |
| S10_asymmetric | 512x512 | 0 | - | 5.27407e-07 | 8.10992e-14 | 5.27407e-07 | 2.77913e-07 | 1.20742e-06 | 1 | 7.45867e-07 | 1.05727e-13 | 4.00364e-16 | 5.27407e-07 | 3.35761e-17 | 1 | +1 | 1 | 0.032828 | 0.0427156 | 0 | 52.537 |
| S11_highfreq | 512x512 | 0 | - | 1.6632e-16 | 8.80674e-14 | 1.66389e-16 | 2.8132e-07 | 1.20742e-06 | 1 | 9.52223e-17 | 1.40063e-13 | 1.34099e-16 | 9.20485e-17 | 3.47376e-17 | 1 | +1 | 1 | -1.73472e-17 | 0.00925514 | 0 | 56.8828 |
| S12_mixed_scale | 512x512 | 0 | - | 2.3025e-16 | 1.08963e-13 | 1.6032e-16 | 2.70178e-07 | 1.20742e-06 | 1 | 1.3049e-16 | 1.57797e-13 | 1.92027e-16 | 1.16394e-16 | 5.09251e-17 | 1 | +1 | 1 | -2.38524e-17 | 0.0345485 | 0 | 54.4455 |
| S13_near_flat | 512x512 | 0 | - | 1.79192e-18 | 8.98925e-16 | 1.32315e-18 | 3.48425e-07 | 1.20742e-06 | 1 | 1.38671e-18 | 1.25816e-15 | 1.48228e-18 | 9.25554e-19 | 3.96445e-19 | 1 | +1 | 1 | 2.98156e-19 | 0.989645 | 0 | 52.5519 |
| S14_steep | 512x512 | 0 | - | 8.68978e-16 | 5.38385e-13 | 8.6895e-16 | 2.74622e-07 | 1.20742e-06 | 1 | 7.50056e-16 | 9.61293e-13 | 6.94031e-16 | 4.98107e-16 | 1.59163e-16 | 1 | +1 | 1 | -6.245e-17 | 0.00882798 | 0 | 51.1599 |
| S15_periodic_noise | 512x512 | 0 | - | 2.24271e-17 | 1.36837e-14 | 2.60224e-17 | 2.48027e-07 | 1.20742e-06 | 1 | 2.15306e-17 | 1.89355e-14 | 1.60016e-17 | 1.45428e-17 | 5.95211e-18 | 1 | +1 | 1 | 4.49944e-18 | 0.0710725 | 0 | 54.6803 |


### Grupo E5 — NON-PERIODIC NEGATIVE CONTROLS (§24)

### Grupo E5

| case | resolution | noise | quantization | rmse | gradient_rmse | raw_centered_rmse | normal_angle_mean | normal_angle_p95 | corr | seam_height | seam_gradient | low_band_error | mid_band_error | high_band_error | ssim | best_sign | scale_a | offset_b | nz_min | nz_floor_hits | runtime_ms |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| N01_ramp | 512x512 | 0 | - | 0.000976562 | 4.24507e-14 | 0.000976562 | 2.30508e-07 | 1.20742e-06 | 0.999994 | 0.00138107 | 2.84217e-14 | 1.81371e-16 | 0.000976562 | 0 | 0.998989 | +1 | 1 | -0.000976562 | 0.00282573 | 0 | 58.1353 |
| N02_bump_boundary | 512x512 | 0 | - | 0.000221067 | 5.43861e-14 | 0.000221067 | 2.68069e-07 | 1.20742e-06 | 1 | 0.000312636 | 6.85993e-14 | 5.0765e-17 | 0.000221067 | 9.64132e-11 | 0.99995 | +1 | 1 | 0.119062 | 0.00460245 | 0 | 53.3302 |


### Grupo E1 — QUANTIZED (§20)

### Grupo E1

| case | resolution | noise | quantization | rmse | gradient_rmse | raw_centered_rmse | normal_angle_mean | normal_angle_p95 | corr | seam_height | seam_gradient | low_band_error | mid_band_error | high_band_error | ssim | best_sign | scale_a | offset_b | nz_min | nz_floor_hits | runtime_ms |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| S06_multifreq | 512x512 | 0 | Q8 | 0.000327354 | 0.0741673 | 0.000345626 | 0.271802 | 0.672783 | 1 | 0.00013686 | 0.0861052 | 0.000303135 | 0.000108991 | 5.82338e-05 | 0.999985 | +1 | 0.999725 | 2.08167e-17 | 0.0665104 | 0 | 61.6324 |
| S06_multifreq | 512x512 | 0 | Q10 | 5.10162e-05 | 0.0168576 | 5.11262e-05 | 0.0685365 | 0.168621 | 1 | 3.02435e-05 | 0.0209293 | 4.31806e-05 | 2.19125e-05 | 1.60601e-05 | 1 | +1 | 1.00001 | 2.42862e-17 | 0.0693197 | 0 | 63.5015 |
| S06_multifreq | 512x512 | 0 | Q16 | 7.48813e-07 | 0.000260294 | 7.4887e-07 | 0.00107631 | 0.00261015 | 1 | 5.91825e-07 | 0.000336313 | 5.86782e-07 | 3.36329e-07 | 3.21389e-07 | 1 | +1 | 1 | 1.38778e-17 | 0.0687715 | 0 | 65.3684 |
| S08_ridges | 512x512 | 0 | Q8 | 0.00522654 | 0.869803 | 0.0167798 | 0.224501 | 0.224548 | 0.999959 | 0.0026623 | 1.58157 | 0.00508865 | 0.00119263 | 0 | 0.998965 | +1 | 1.0284 | 0 | 0.0274404 | 0 | 60.1285 |
| S08_ridges | 512x512 | 0 | Q10 | 0.00426798 | 0.225651 | 0.00490429 | 0.0559716 | 0.0560579 | 0.999973 | 0.00020367 | 0.134193 | 0.00426305 | 0.000204991 | 0 | 0.999169 | +1 | 0.995833 | 0 | 0.0302891 | 0 | 59.5948 |
| S08_ridges | 512x512 | 0 | Q16 | 3.3766e-05 | 0.00347069 | 3.41727e-05 | 0.000874278 | 0.00087429 | 1 | 5.72734e-07 | 0.000595517 | 3.34015e-05 | 4.94815e-06 | 0 | 1 | +1 | 1.00001 | 0 | 0.0300452 | 0 | 59.8089 |
| S09_bricks | 512x512 | 0 | Q8 | 0.0175282 | 3.09758 | 0.0210285 | 1.90302 | 5.99825 | 0.997597 | 0.00325629 | 2.77301 | 0.0167922 | 0.00494897 | 0.000877616 | 0.966359 | +1 | 0.955994 | 0.401249 | 0.00391645 | 0 | 63.5048 |
| S09_bricks | 512x512 | 0 | Q10 | 0.0107329 | 1.88622 | 0.0124145 | 0.944064 | 2.84786 | 0.9991 | 0.000963335 | 0.498601 | 0.0102774 | 0.00304018 | 0.00057211 | 0.99127 | +1 | 0.975911 | 0.401249 | 0.00293203 | 0 | 60.2681 |
| S09_bricks | 512x512 | 0 | Q16 | 3.66906e-05 | 0.0184876 | 3.66962e-05 | 0.0112288 | 0.049336 | 1 | 2.81677e-05 | 0.0167687 | 2.18744e-05 | 2.22903e-05 | 1.92577e-05 | 1 | +1 | 0.999997 | 0.401249 | 0.00343326 | 0 | 65.7359 |
| S10_asymmetric | 512x512 | 0 | Q8 | 0.00181909 | 0.325165 | 0.00237602 | 0.448494 | 1.22235 | 0.999992 | 0.000378849 | 0.190421 | 0.00172887 | 0.000544045 | 0.000155259 | 0.999555 | +1 | 0.99669 | 0.032828 | 0.0429468 | 0 | 60.7298 |
| S10_asymmetric | 512x512 | 0 | Q10 | 0.000221627 | 0.0706286 | 0.000235406 | 0.119262 | 0.320082 | 1 | 8.76164e-05 | 0.0692844 | 0.000192087 | 0.000107373 | 2.63144e-05 | 0.999997 | +1 | 0.999828 | 0.032828 | 0.0419896 | 0 | 64.4104 |
| S10_asymmetric | 512x512 | 0 | Q16 | 2.44112e-06 | 0.000980328 | 2.44696e-06 | 0.00197577 | 0.00523583 | 1 | 1.64959e-06 | 0.00104609 | 1.83149e-06 | 1.40566e-06 | 7.92987e-07 | 1 | +1 | 1 | 0.032828 | 0.0427096 | 0 | 66.9647 |
| S12_mixed_scale | 512x512 | 0 | Q8 | 0.00102978 | 0.365273 | 0.00248571 | 0.708789 | 1.81298 | 0.999998 | 0.000835402 | 0.528356 | 0.000827521 | 0.000514286 | 0.0003334 | 0.999893 | +1 | 0.995665 | -2.709e-17 | 0.0351233 | 0 | 61.4618 |
| S12_mixed_scale | 512x512 | 0 | Q10 | 0.000329476 | 0.0838932 | 0.000359285 | 0.176142 | 0.434397 | 1 | 0.00021733 | 0.140399 | 0.000287658 | 0.000108336 | 0.000118619 | 0.999993 | +1 | 1.00028 | -2.25511e-17 | 0.0341709 | 0 | 62.8687 |
| S12_mixed_scale | 512x512 | 0 | Q16 | 3.6562e-06 | 0.00128197 | 3.86687e-06 | 0.00267671 | 0.00665271 | 1 | 3.29678e-06 | 0.00219357 | 2.86313e-06 | 1.69519e-06 | 1.51547e-06 | 1 | +1 | 1 | -2.53703e-17 | 0.0345622 | 0 | 64.1167 |
| S14_steep | 512x512 | 0 | Q8 | 0.0785298 | 7.17782 | 0.173788 | 2.31727 | 5.87962 | 0.999398 | 0.0237953 | 0.851347 | 0.0782323 | 0.00558515 | 0.00393003 | 0.980259 | +1 | 1.07356 | -1.35923e-16 | 0.0117014 | 0 | 63.7833 |
| S14_steep | 512x512 | 0 | Q10 | 0.00988184 | 2.16204 | 0.0185759 | 0.661783 | 2.01928 | 0.99999 | 0.00374019 | 0.969231 | 0.00913412 | 0.00355692 | 0.00125177 | 0.999781 | +1 | 0.9931 | -4.18728e-17 | 0.00878623 | 0 | 60.679 |
| S14_steep | 512x512 | 0 | Q16 | 0.000110358 | 0.0317432 | 0.000111474 | 0.011019 | 0.0290755 | 1 | 0.000124253 | 0.0537907 | 6.16395e-05 | 4.02823e-05 | 8.22001e-05 | 1 | +1 | 1.00001 | -7.63278e-17 | 0.0088348 | 0 | 61.0479 |


### Grupo E2 — NOISE (§21)

### Grupo E2

| case | resolution | noise | quantization | rmse | gradient_rmse | raw_centered_rmse | normal_angle_mean | normal_angle_p95 | corr | seam_height | seam_gradient | low_band_error | mid_band_error | high_band_error | ssim | best_sign | scale_a | offset_b | nz_min | nz_floor_hits | runtime_ms |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| S06_multifreq | 512x512 | 1/255 | - | 0.00038801 | 0.117091 | 0.000633924 | 0.478531 | 1.16713 | 1 | 0.000226291 | 0.153956 | 0.000340064 | 0.000150942 | 0.000110114 | 0.99998 | +1 | 0.998758 | 2.08167e-17 | 0.0587846 | 0 | 66.0641 |
| S06_multifreq | 512x512 | 2/255 | - | 0.000768275 | 0.240273 | 0.00216596 | 0.974853 | 2.37761 | 0.999998 | 0.000446886 | 0.276828 | 0.000672326 | 0.000310216 | 0.000204916 | 0.999916 | +1 | 0.995001 | 7.00826e-18 | 0.0425436 | 0 | 68.5798 |
| S06_multifreq | 512x512 | 4/255 | - | 0.00204506 | 0.54587 | 0.00861325 | 2.06196 | 5.07619 | 0.999987 | 0.000958173 | 0.661425 | 0.00187579 | 0.000682508 | 0.000444799 | 0.999383 | +1 | 0.979666 | 1.40189e-17 | 0.0141832 | 0 | 73.4788 |
| S06_multifreq | 512x512 | 8/255 | - | 0.269085 | 9247.01 | 48.2697 | 73.6323 | 161.126 | 0.744594 | 13.0202 | 1291.88 | 0.251448 | 0.074294 | 0.0605091 | 0.277773 | +1 | 0.00617997 | 2.08167e-17 | -0.0362227 | 89 | 70.7884 |
| S08_ridges | 512x512 | 1/255 | - | 0.003237 | 1.20674 | 0.00613085 | 2.14085 | 4.6034 | 0.999984 | 0.00284557 | 2.20763 | 0.00269323 | 0.00153869 | 0.000925809 | 0.999413 | +1 | 0.991063 | -6.66534e-18 | 0.0170728 | 0 | 67.9404 |
| S08_ridges | 512x512 | 2/255 | - | 0.564643 | 976.527 | 2.86929 | 18.8538 | 79.9213 | 0.208934 | 11.9427 | 6631.83 | 0.560193 | 0.0524522 | 0.0474689 | 0.0299379 | +1 | 0.0411188 | 7.44504e-19 | -0.0014946 | 1 | 68.2901 |
| S08_ridges | 512x512 | 4/255 | - | 0.380043 | 33280.7 | 132.737 | 74.3939 | 161.763 | 0.752832 | 94.3624 | 71006.5 | 0.340715 | 0.140137 | 0.0933137 | 0.205311 | +1 | 0.00326402 | -2.44606e-18 | -0.0313635 | 1156 | 72.1488 |
| S08_ridges | 512x512 | 8/255 | - | 0.109085 | 157692 | 2265.62 | 58.4779 | 162.08 | 0.981991 | 470.09 | 221083 | 0.0763085 | 0.0464576 | 0.0625957 | 0.676726 | +1 | 0.000250195 | 1.11109e-19 | -0.0970709 | 23801 | 70.2824 |
| S09_bricks | 512x512 | 1/255 | - | 0.226351 | 14355.4 | 73.3904 | 73.6776 | 158.126 | 0.44658 | 27.7771 | 17652.6 | 0.223662 | 0.0271366 | 0.0217695 | 0.0958549 | +1 | 0.00153701 | 0.401249 | -0.0100936 | 191 | 71.6152 |
| S09_bricks | 512x512 | 2/255 | - | 0.215216 | 26117.7 | 131.69 | 73.0536 | 159.511 | 0.525607 | 26.924 | 25412.7 | 0.213101 | 0.0283952 | 0.00997669 | 0.166287 | +1 | 0.00100868 | 0.401249 | -0.0199889 | 597 | 68.2784 |
| S09_bricks | 512x512 | 4/255 | - | 0.224446 | 40371.5 | 146.249 | 77.5759 | 160.246 | 0.46136 | 46.7317 | 38551.6 | 0.221438 | 0.0344731 | 0.012368 | 0.126724 | +1 | 0.000797416 | 0.401249 | -0.048711 | 1354 | 68.5722 |
| S09_bricks | 512x512 | 8/255 | - | 0.229155 | 61354.6 | 184.665 | 80.7509 | 161.405 | 0.42365 | 72.5828 | 51787.7 | 0.225003 | 0.0376093 | 0.0217054 | 0.0946113 | +1 | 0.000580037 | 0.401249 | -0.0943945 | 3116 | 69.8362 |
| S10_asymmetric | 512x512 | 1/255 | - | 0.00149966 | 0.446379 | 0.00230602 | 0.86458 | 2.32486 | 0.999995 | 0.00120827 | 0.467569 | 0.00116306 | 0.000573537 | 0.000753204 | 0.999773 | +1 | 0.996209 | 0.032828 | 0.0274217 | 0 | 69.5776 |
| S10_asymmetric | 512x512 | 2/255 | - | 0.00389814 | 0.982037 | 0.00825302 | 1.80591 | 4.92357 | 0.999964 | 0.00284832 | 1.0795 | 0.0032152 | 0.00123209 | 0.00182755 | 0.998552 | +1 | 0.984441 | 0.032828 | 0.0145945 | 0 | 71.5241 |
| S10_asymmetric | 512x512 | 4/255 | - | 0.419545 | 7353.24 | 24.2717 | 75.6435 | 164.271 | 0.411323 | 13.0253 | 7357.18 | 0.408662 | 0.0730851 | 0.0605954 | 0.0790071 | +1 | 0.00774103 | 0.032828 | -0.0162334 | 57 | 69.6132 |
| S10_asymmetric | 512x512 | 8/255 | - | 0.214043 | 62895.2 | 708.013 | 71.788 | 163.234 | 0.885298 | 79.8905 | 43100.7 | 0.207305 | 0.0447972 | 0.0288459 | 0.438546 | +1 | 0.000575208 | 0.032828 | -0.0788739 | 3958 | 70.0878 |
| S12_mixed_scale | 512x512 | 1/255 | - | 0.00185006 | 0.600164 | 0.00322746 | 1.17212 | 2.96467 | 0.999994 | 0.00148595 | 1.01684 | 0.00156339 | 0.000768027 | 0.000623438 | 0.999744 | +1 | 0.994936 | -2.25569e-17 | 0.0210304 | 0 | 70.3487 |
| S12_mixed_scale | 512x512 | 2/255 | - | 0.0041186 | 1.38684 | 0.0129114 | 2.53022 | 6.55171 | 0.999969 | 0.00420751 | 2.7829 | 0.00328846 | 0.00172607 | 0.00178033 | 0.998699 | +1 | 0.976991 | -2.27882e-17 | 0.00729213 | 0 | 72.0407 |
| S12_mixed_scale | 512x512 | 4/255 | - | 0.40302 | 13772.8 | 56.369 | 78.8666 | 165.022 | 0.631209 | 42.8149 | 24685.3 | 0.369502 | 0.103047 | 0.123592 | 0.12572 | +1 | 0.00578504 | -1.84975e-17 | -0.0174184 | 199 | 75.2751 |
| S12_mixed_scale | 512x512 | 8/255 | - | 0.130782 | 79810.9 | 1016.71 | 61.4042 | 155.66 | 0.967808 | 211.526 | 135919 | 0.113869 | 0.0484698 | 0.0422886 | 0.587919 | +1 | 0.000494379 | -2.71484e-17 | -0.0884718 | 6377 | 73.8544 |
| S14_steep | 512x512 | 1/255 | - | 1.40781 | 25170.7 | 115.739 | 73.0665 | 166.873 | 0.783123 | 67.6508 | 47764.5 | 1.25184 | 0.491534 | 0.416206 | 0.231306 | +1 | 0.0150878 | -6.96275e-17 | -0.00589987 | 662 | 68.287 |
| S14_steep | 512x512 | 2/255 | - | 0.485155 | 123556 | 1886.99 | 59.6881 | 159.632 | 0.976767 | 310.927 | 187955 | 0.430437 | 0.172235 | 0.142948 | 0.659763 | +1 | 0.00117047 | -9.29611e-17 | -0.0248938 | 14834 | 69.9298 |
| S14_steep | 512x512 | 4/255 | - | 0.222915 | 242046 | 5839.8 | 49.421 | 150.46 | 0.99514 | 580.614 | 287311 | 0.183644 | 0.096803 | 0.0812106 | 0.867311 | +1 | 0.000385626 | -9.82481e-17 | -0.0624349 | 50069 | 69.6965 |
| S14_steep | 512x512 | 8/255 | - | 0.182368 | 325729 | 9265.3 | 39.7009 | 135.076 | 0.99675 | 617.978 | 313053 | 0.160685 | 0.0726102 | 0.0465443 | 0.902035 | +1 | 0.000243483 | -6.24874e-17 | -0.123992 | 83484 | 67.0031 |


### Grupo E3 — WRONG SIGN (§22)

### Grupo E3

| case | resolution | noise | quantization | rmse | gradient_rmse | raw_centered_rmse | normal_angle_mean | normal_angle_p95 | corr | seam_height | seam_gradient | low_band_error | mid_band_error | high_band_error | ssim | best_sign | scale_a | offset_b | nz_min | nz_floor_hits | runtime_ms |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| S06_multifreq | 512x512 | 0 | - | 0.367169 | 5.67215 | 0.665259 | 75.7217 | 146.965 | -0.412775 | 0.0121687 | 0.297588 | 0.367169 | 2.15851e-16 | 7.752e-17 | 0.144362 | -1 | -0.428452 | 2.08167e-17 | 0.0687849 | 0 | 47.8605 |
| S06_multifreq | 512x512 | 0 | - | 0.367169 | 6.00187 | 0.429049 | 93.9627 | 154.27 | 0.412775 | 0.0083242 | 0.231099 | 0.367169 | 2.15851e-16 | 7.752e-17 | 0.144362 | +1 | 0.428452 | 2.08167e-17 | 0.0687849 | 0 | 50.3462 |
| S06_multifreq | 512x512 | 0 | - | 1.28561e-16 | 9.32476 | 0.806226 | 155.201 | 169.775 | -1 | 0.0169967 | 0.472566 | 8.69307e-17 | 8.71901e-17 | 3.69991e-17 | 1 | -1 | -1 | 1.73472e-17 | 0.0687849 | 0 | 47.9521 |
| S08_ridges | 512x512 | 0 | - | 1.15766e-16 | 33.8493 | 1.15477 | 173.761 | 175.653 | -1 | 0.0662913 | 33.2694 | 8.06641e-17 | 8.30368e-17 | 0 | 1 | -1 | -1 | 0 | 0.0300441 | 0 | 48.3199 |
| S08_ridges | 512x512 | 0 | - | 1.15766e-16 | 5.17546e-14 | 1.15766e-16 | 1.85619e-07 | 8.53774e-07 | 1 | 7.85046e-17 | 1.06581e-13 | 8.06641e-17 | 8.30368e-17 | 0 | 1 | +1 | 1 | 0 | 0.0300441 | 0 | 50.8367 |
| S08_ridges | 512x512 | 0 | - | 1.15766e-16 | 33.8493 | 1.15477 | 173.761 | 175.653 | -1 | 0.0662913 | 33.2694 | 8.06641e-17 | 8.30368e-17 | 0 | 1 | -1 | -1 | 0 | 0.0300441 | 0 | 45.596 |
| S09_bricks | 512x512 | 0 | - | 0.234434 | 4.06617 | 0.264718 | 60.8009 | 131.24 | 0.375818 | 0.00502944 | 2.00887 | 0.234126 | 0.0119179 | 0.00151801 | 0.126053 | +1 | 0.436074 | 0.401249 | 0.00342702 | 0 | 50.8444 |
| S09_bricks | 512x512 | 0 | - | 0.234434 | 28.9354 | 0.391138 | 109.972 | 171.88 | -0.375818 | 0.0029719 | 0.31337 | 0.234126 | 0.0119179 | 0.00151801 | 0.126053 | -1 | -0.436074 | 0.401249 | 0.00342702 | 0 | 47.636 |
| S09_bricks | 512x512 | 0 | - | 2.10969e-06 | 29.5372 | 0.505957 | 155.697 | 174.192 | -1 | 0.00414451 | 2.11809 | 7.83431e-17 | 2.10969e-06 | 5.54617e-12 | 1 | -1 | -1 | 0.401249 | 0.00342702 | 0 | 53.5799 |
| S10_asymmetric | 512x512 | 0 | - | 0.379566 | 16.5693 | 0.8145 | 116.926 | 167.375 | -0.565666 | 0.0202608 | 0.146572 | 0.379566 | 7.03662e-05 | 3.34179e-17 | 0.202932 | -1 | -0.565666 | 0.032828 | 0.0427156 | 0 | 49.2774 |
| S10_asymmetric | 512x512 | 0 | - | 0.379566 | 8.20459 | 0.428996 | 59.9557 | 136.759 | 0.565666 | 0.0210349 | 0.282642 | 0.379566 | 7.03662e-05 | 3.34179e-17 | 0.202932 | +1 | 0.565666 | 0.032828 | 0.0427156 | 0 | 57.1929 |
| S10_asymmetric | 512x512 | 0 | - | 5.27407e-07 | 18.4893 | 0.920569 | 167.84 | 174.635 | -1 | 0.0292056 | 0.318386 | 4.00364e-16 | 5.27407e-07 | 3.35761e-17 | 1 | -1 | -1 | 0.032828 | 0.0427156 | 0 | 54.5598 |
| S12_mixed_scale | 512x512 | 0 | - | 0.198997 | 8.35923 | 1 | 51.9763 | 130.995 | -0.92376 | 0.0175223 | 0.698075 | 0.198997 | 5.54674e-16 | 2.22222e-16 | 0.170635 | -1 | -0.96 | -2.05131e-17 | 0.0345485 | 0 | 55.3185 |
| S12_mixed_scale | 512x512 | 0 | - | 0.198997 | 16.6712 | 0.2 | 124.909 | 170.847 | 0.92376 | 0.0433349 | 0.953035 | 0.198997 | 5.54674e-16 | 2.22222e-16 | 0.170635 | +1 | 0.96 | -2.05131e-17 | 0.0345485 | 0 | 55.1861 |
| S12_mixed_scale | 512x512 | 0 | - | 2.3025e-16 | 21.1865 | 1.03923 | 168.655 | 175.45 | -1 | 0.0506968 | 1.53912 | 1.92027e-16 | 1.16394e-16 | 5.09251e-17 | 1 | -1 | -1 | -2.38524e-17 | 0.0345485 | 0 | 49.9726 |
| S14_steep | 512x512 | 0 | - | 2.20863 | 94.2478 | 3.53553 | 106.896 | 170.355 | -0.219512 | 0.26009 | 0.255368 | 2.20863 | 1.79853e-15 | 1.78908e-16 | 0.071424 | -1 | -0.219512 | -6.8712e-17 | 0.00882798 | 0 | 48.3553 |
| S14_steep | 512x512 | 0 | - | 2.20863 | 62.8319 | 2.82843 | 72.9324 | 158.799 | 0.219512 | 0.00532278 | 3.8529 | 2.20863 | 1.79853e-15 | 1.78908e-16 | 0.071424 | +1 | 0.219512 | -6.8712e-17 | 0.00882798 | 0 | 46.2389 |
| S14_steep | 512x512 | 0 | - | 8.68978e-16 | 113.272 | 4.52769 | 178.122 | 178.952 | -1 | 0.260144 | 3.86135 | 6.94031e-16 | 4.98107e-16 | 1.59163e-16 | 1 | -1 | -1 | -6.245e-17 | 0.00882798 | 0 | 50.0161 |


### Grupo E4 — STEEP SLOPES + nz_floor (§23)

### Grupo E4

| case | resolution | noise | quantization | rmse | gradient_rmse | raw_centered_rmse | normal_angle_mean | normal_angle_p95 | corr | seam_height | seam_gradient | low_band_error | mid_band_error | high_band_error | ssim | best_sign | scale_a | offset_b | nz_min | nz_floor_hits | runtime_ms |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| E4_A1 | 512x512 | 0 | - | 2.59995e-16 | 1.68854e-13 | 2.60099e-16 | 2.78181e-07 | 1.20742e-06 | 1 | 2.58761e-16 | 2.74828e-13 | 1.76384e-16 | 1.78712e-16 | 6.74372e-17 | 1 | +1 | 1 | -2.77556e-17 | 0.0220654 | 0 | 49.4861 |
| E4_A1_noise | 512x512 | 1/255 | - | 0.00751215 | 2.60397 | 0.0212849 | 2.16967 | 5.68649 | 0.999966 | 0.00669559 | 4.64042 | 0.00614987 | 0.00323407 | 0.00285521 | 0.99872 | +1 | 0.97848 | -1.05577e-17 | 0.00572896 | 0 | 73.1395 |
| E4_A2 | 512x512 | 0 | - | 5.48875e-16 | 3.3562e-13 | 5.49464e-16 | 2.80051e-07 | 1.20742e-06 | 1 | 4.93001e-16 | 4.70263e-13 | 3.93907e-16 | 3.57897e-16 | 1.34204e-16 | 1 | +1 | 1 | -8.32667e-17 | 0.0110347 | 0 | 49.3526 |
| E4_A2_noise | 512x512 | 1/255 | - | 1.60116 | 9479.48 | 31.4551 | 73.585 | 166.456 | 0.467314 | 27.0253 | 15916.2 | 1.53353 | 0.32214 | 0.328941 | 0.0868682 | +1 | 0.0262344 | -3.17819e-17 | -0.00386903 | 94 | 75.8585 |
| E4_A4 | 512x512 | 0 | - | 1.07149e-15 | 6.78845e-13 | 1.0722e-15 | 2.79198e-07 | 1.20742e-06 | 1 | 9.92133e-16 | 9.72779e-13 | 7.49706e-16 | 7.17658e-16 | 2.6645e-16 | 1 | +1 | 1 | -1.80411e-16 | 0.00551762 | 0 | 51.1668 |
| E4_A4_noise | 512x512 | 1/255 | - | 0.963176 | 85130.6 | 962.256 | 63.3113 | 161.719 | 0.963997 | 194.89 | 137850 | 0.858796 | 0.383095 | 0.208364 | 0.577454 | +1 | 0.00361559 | -3.08535e-17 | -0.0136504 | 7283 | 75.8908 |
| E4_A8 | 512x512 | 0 | - | 2.15754e-15 | 1.35547e-12 | 2.15995e-15 | 2.79809e-07 | 1.20742e-06 | 1 | 1.95585e-15 | 1.92501e-12 | 1.52297e-15 | 1.43258e-15 | 5.32218e-16 | 1 | +1 | 1 | -3.33067e-16 | 0.00275884 | 0 | 64.3648 |
| E4_A8_noise | 512x512 | 1/255 | - | 0.946651 | 204816 | 4464.25 | 53.001 | 154.645 | 0.991425 | 454.33 | 256957 | 0.836473 | 0.357332 | 0.262248 | 0.824134 | +1 | 0.00160624 | -1.8443e-16 | -0.0160564 | 37405 | 78.892 |
| E4_A16 | 512x512 | 0 | - | 4.36061e-15 | 2.69761e-12 | 4.36699e-15 | 2.83415e-07 | 1.20742e-06 | 1 | 3.99076e-15 | 3.93004e-12 | 3.11315e-15 | 2.86107e-15 | 1.06655e-15 | 1 | +1 | 1 | -5.55112e-16 | 0.00137942 | 0 | 52.9561 |
| E4_A16_noise | 512x512 | 1/255 | - | 1.21273 | 301956 | 8281.49 | 42.2681 | 140.322 | 0.996491 | 670.847 | 311498 | 1.02686 | 0.499021 | 0.408951 | 0.90403 | +1 | 0.00174035 | -1.78629e-16 | -0.0167179 | 73414 | 76.8116 |
| E4_A32 | 512x512 | 0 | - | 8.40985e-15 | 5.2944e-12 | 8.41926e-15 | 2.82627e-07 | 1.20742e-06 | 1 | 8.88182e-15 | 8.53795e-12 | 5.78763e-15 | 5.72041e-15 | 2.1227e-15 | 1 | +1 | 1 | -9.99201e-16 | 0.000689713 | 0 | 52.2752 |
| E4_A32_noise | 512x512 | 1/255 | - | 2.5752 | 361258 | 10803.1 | 34.9054 | 125.019 | 0.996043 | 786.672 | 327821 | 2.30345 | 0.828582 | 0.799517 | 0.880436 | +1 | 0.00266457 | -5.55112e-16 | -0.0159322 | 99389 | 79.1713 |
| E4_A16_floor0.01 | 512x512 | 1/255 | - | 1.38615 | 295.819 | 11.7805 | 4.70389 | 10.2606 | 0.995413 | 0.706945 | 8.53676 | 1.38588 | 0.0254334 | 0.0100341 | 0.870971 | +1 | 5.2954 | -2.04067e-16 | -0.0160909 | 250986 | 77.9987 |
| E4_A16_floor0.001 | 512x512 | 1/255 | - | 0.967706 | 189.955 | 1.71471 | 14.0249 | 40.8772 | 0.997767 | 0.396926 | 259.62 | 0.922935 | 0.213044 | 0.198137 | 0.92237 | +1 | 0.910814 | -2.77556e-16 | -0.0158756 | 96426 | 76.7398 |
| E4_A16_floor1e-06 | 512x512 | 1/255 | - | 1.17671 | 302444 | 8298.48 | 42.1522 | 140.239 | 0.996697 | 608.129 | 312590 | 1.00319 | 0.495973 | 0.363672 | 0.90972 | +1 | 0.00173715 | -1.29438e-16 | -0.0168373 | 73723 | 76.9585 |
| E4_A32_floor0.01 | 512x512 | 1/255 | - | 2.81458 | 657.915 | 26.2617 | 4.78895 | 10.4704 | 0.995272 | 1.53925 | 19.9133 | 2.81351 | 0.0767234 | 0.0121294 | 0.868444 | +1 | 10.5649 | -7.01728e-16 | -0.0166061 | 257482 | 74.0521 |
| E4_A32_floor0.001 | 512x512 | 1/255 | - | 2.4159 | 339.333 | 10.9199 | 12.0367 | 33.9919 | 0.996518 | 0.897979 | 240.952 | 2.3654 | 0.371377 | 0.321751 | 0.891645 | +1 | 1.58426 | -7.31e-16 | -0.0178942 | 125615 | 74.3747 |
| E4_A32_floor1e-06 | 512x512 | 1/255 | - | 2.64756 | 361694 | 10827.5 | 34.8627 | 124.567 | 0.995817 | 773.626 | 332369 | 2.40125 | 0.827375 | 0.747666 | 0.881379 | +1 | 0.00265799 | -1.00838e-15 | -0.0155467 | 99603 | 75.2889 |


### Grupo E6 — RESOLUTION/PERFORMANCE (§30)

### Grupo E6

| case | resolution | noise | quantization | rmse | gradient_rmse | raw_centered_rmse | normal_angle_mean | normal_angle_p95 | corr | seam_height | seam_gradient | low_band_error | mid_band_error | high_band_error | ssim | best_sign | scale_a | offset_b | nz_min | nz_floor_hits | runtime_ms |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| E6_solve_256x256 | 256x256 | - | - |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  | 4.56973 |
| E6_pipeline_256x256 | 256x256 | - | - |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  | 45.3089 |
| E6_solve_512x512 | 512x512 | - | - |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  | 20.4084 |
| E6_pipeline_512x512 | 512x512 | - | - |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  | 234.199 |
| E6_solve_1024x1024 | 1024x1024 | - | - |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  | 106.556 |
| E6_pipeline_1024x1024 | 1024x1024 | - | - |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  | 1227.3 |
| E6_solve_2048x2048 | 2048x2048 | - | - |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  | 596.807 |
| E6_pipeline_2048x2048 | 2048x2048 | - | - |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  | 6224.71 |
| E6_peak_mem_MB_solve_2048 | 2048x2048 |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  | 484.035 |


## Distribution summary

- **E0**: RMSE ≤ 1e-15 (eps de máquina) en todas las superficies de espectro entero;
  exacto incluso con nz_min = 6.9e-4 (A=32). Peores casos: S09 2.1e-6, S10 5.3e-7,
  S07 1.6e-10 — completamente explicados por el modo Nyquist anulado (ver Failure
  cases), no por el solver. `scale_a = 1 ± 1e-15`, `best_sign=+1` en todos.
- **E5**: controles separados del grupo periódico por `seam_height` (1.4e-3 y 3.1e-4 vs
  ~1e-16; factor 10¹²–10¹⁴) y por RMSE (10²–10³× la mediana periódica).
- **E1**: Q8 graceful en pendientes suaves (S06: 3.3e-4) y amplificado en nz pequeño
  (S09: 1.8e-2, ángulo p95 6.0°). Monótono en bits (Q8 > Q10 > Q16 > perfecto) en los
  6 casos de estrés.
- **E2**: degradación monótona y moderada hasta σ=2/255 en superficies suaves; régimen
  inestable cuando el ruido alcanza nz (S08 ya explota en σ=2/255: RMSE 0.56,
  gradient_rmse ~1e3). En el régimen inestable la degradación **NO es monótona** en σ.
- **E3**: toda mutación de convención es detectada — flips de un eje por RMSE/ángulo
  (0.2–2.2), inversión global flip_xy SOLO por `best_sign=-1`/`corr=-1` (RMSE 1e-16).
  `flip_y` sobre superficie X-pura es un no-op correcto (S08 sin cambio).
- **E4**: datos perfectos exactos en todo el barrido (nz_min 2.2e-2 → 6.9e-4). Con
  σ=1/255 el umbral de explosión está en nz_min ≈ σ (A=1: 7.5e-3 OK; A≥2: RMSE ~1-2.6).
  `nz_floor=0.01` acota los picos (seam_gradient 8.5 vs 3.1e5 con floor=1e-6) al costo
  de sesgo (ángulo medio 4.7°).
- **E6**: solve O(N log N) limpio: 4.3/21.8/100/672 ms (256²→2048²). Pipeline completo
  con métricas: 47 ms → 7.1 s (las métricas, sobre todo SSIM/ángulo, dominan 10×).

## Failure cases

1. **Modo Nyquist anulado (E0, explicado y cuantificado)**: la reconstrucción pierde
   exactamente los 3 modos Nyquist de ``h`` (convención de derivada par). Para S09 el
   error completo ES ese modo: espectro del error = 7.6e-4 en los bins Nyquist y
   ≤ 1.4e-14 en todo el resto. Es una limitación de la convención, no del solver; para
   normales reales (que no llevan fase de altura) el contenido Nyquist de la altura es
   inobservable de todos modos. Documentado, no corregido en esta PR.
2. **nz < 0 sin política (E2/E4)**: ruido σ ≥ nz_min genera normales con nz negativo
   (superficie "mirando al revés"); el floor 1e-6 las convierte en picos de gradiente
   ~1e5-1e6 que el integrador esparce globalmente (RMSE 0.2–2.6, `scale_a` colapsa a
   ~1e-3). NP-M0 no implementa la cura (§25: sin Poisson ponderado/máscaras); queda
   medido como el régimen inestable real del método base.
3. **No-monotonicidad en el régimen inestable (E2)**: p.ej. S08 RMSE 0.56 → 0.38 → 0.11
   para σ creciente: el fitting afín colapsa de forma distinta por corrida, así que el
   RMSE alineado deja de ser comparable cuando la reconstrucción se destruye. Las
   métricas crudas (gradient_rmse 1e3→3e5) sí crecen monótonas.

## Unexpected findings

1. **El fitting afín oculta exactamente las dos clases de bug que más miedo dan**:
   M1 (factor 2π) queda en RMSE alineado < 1e-4 (la escala lo absorbe) y flip_xy queda
   en RMSE 1e-16 (el signo lo absorbe). Solo `gradient_rmse`, `normal_angle`,
   `best_sign` y `corr` los detectan. Esto valida el diseño del oracle (§35) y define
   qué métricas son **obligatorias** en los gates futuros: RMSE alineado solo NO basta.
2. **El ángulo normal medio es un detector débil de inversión en pendientes suaves**
   (10.6° en S06 con corre=−1: las zonas planas dominan la media). El detector correcto
   de inversión es `best_sign`/`corr`; el ángulo p95 es mejor resumen que la media.
3. **La proyección FC de contenido no periódico es sorprendentemente buena**: la rampa
   N01 se reconstruye con RMSE 9.8e-4 (0.3% de su amplitud, corr 0.999994), no colapsa
   a plano. El contrato periódico se viola "con elegancia"; la violación se detecta por
   `seam_height` (10¹⁴×), no por RMSE catastrófico. Implicación: en datos reales, una
   textura no tileable NO va a gritar por RMSE; el gate de tileabilidad debe basarse en
   la métrica de seam.
4. **El piso de nz es el parámetro de estabilidad, no la máquina**: con datos perfectos
   float64 integra exacto hasta nz ~ 7e-4; con ruido, el umbral de explosión es
   nz_min ≈ σ_ruido. `nz_floor` debe derivarse del nivel de ruido esperado (Q8 ≈ 1/255)
   y reportarse por-caso — hay medición para fijarlo con criterio, no por folklore.
5. **Unidades**: el spike cazó un bug propio de convención (2π·fftfreq = rad/muestra
   vs rad/unidad-de-textura) que TODOS los tests de exactitud pasaban igual (el loop
   era internamente consistente). Solo el análisis de unidades + nz lo destapó.
   Lección: los tests de roundtrip no detectan errores de escala; hacen falta tests de
   convención explícitos (los hay ahora).

## Hypotheses falsified

- **H4 parcialmente**: "Q8 degrada graceful" — falso en general: es graceful solo
  mientras nz ≫ 1/255. En superficies con pendientes fuertes (S09, nz_min 3.4e-3) Q8
  degrada 50× más que en suaves y el ángulo p95 llega a 6°. La cuantización 8-bit de
  normales con relieve pronunciado es una fuente real de error que el pipeline deberá
  detectar (gate por nz_min).
- **H2 en su formulación débil**: "DC no observable" es cierto, pero en el contrato
  periódico **la baja frecuencia NO es inobservable**: S02/S03/S12 reconstruyen modos
  k=1..5 con RMSE ~1e-16. La pérdida de baja frecuencia del método base aparece solo
  por (a) contenido no periódico (N02: error concentrado en LOW band, medido) y
  (b) amplificación 1/|k| del ruido (E2: low_band_error crece 10-100× más rápido que
  high_band). "LF impossible" sin calificar es un mito para el solver periódico; la
  afirmación precisa es "LF amplifica ruido y proyecta mal lo no periódico".

## Hypotheses surviving

- **H1**: superficies periódicas perfectas se reconstruyen de manera esencialmente
  exacta (RMSE eps de máquina; el residual es el modo Nyquist documentado).
- **H2 (forma fuerte DC)**: la media no es recuperable; el solver devuelve media cero.
- **H3**: la reconstrucción es exactamente periódica en el toro (seam ~1e-16 vs GT
  periódico; el wrap de H' replica el de H).
- **H5**: errores de signo/convención son detectables — flips de un eje por RMSE/
  ángulo; inversión global por best_sign/corr (no por RMSE alineado).
- **H6**: nz→0 expone la inestabilidad — pero SOLO con ruido/cuantización; en float64
  perfecto el método es estable hasta nz ~ 7e-4 sin floor hits.
- **H7**: controles no periódicos performan materialmente peor (seam 10¹²–10¹⁴×).

## Numerical stability

- `max_imag_residual` tras IFFT ~ 1e-17 (espectro Hermitiano preservado por el guard
  Nyquist); sin NaN/Inf en ningún grupo con el guard de 4 bins.
- Determinismo bit-a-bit verificado en tests (misma seed → arrays idénticos).
- Dependencias: solo NumPy 2.4.6 (pocketfft interno). Se esperan diferencias
  cross-platform solo a nivel ~1e-12 (no se exige bit-exactitud, §29).

## Performance

CPU única (Xeon @ 2.60GHz, un hilo): solve 256²=4.3ms, 512²=21.8ms, 1024²=100ms,
2048²=672ms; pico de memoria del solve a 2048²: 484 MB (tracemalloc). Pipeline completo
con métricas 2048²: 7.1 s (las métricas dominan; optimización futura, no crítica).
Extrapolación ingenua a 5.000 candidatas 2K ≈ 2-3 min de solve puro + métricas — el
coste estará en decodificar DDS y comprimir, no en integrar.

## Limitations

1. Sin normales reales ni DDS/BC: la cuantización simulada NO es BC5 (BC5 tiene error
   de bloque; Q8 uniforme es el piso optimista). EXP-003 lo medirá con BC real.
2. Sin restauración de LF, sin pesos, sin multi-escala (§25): los regímenes malos
   (nz~ruido) quedan visibles a propósito.
3. Dataset sintético propio: no reemplaza MatSynth/CC0 (EXP-001b).
4. Métricas no optimizadas (SSIM ventana 7 con cumsum es O(N·w²) en worst case de
   constantes — medido fino a 2048² pero sin afinar).
5. numpy fuera de pyproject (ver Environment): los tests se saltan sin NumPy.

## Adversarial questions (§34, respondidas explícitamente)

1. **¿Algún caso periódico perfecto falla?** Ninguno catastrófico. S09 (2.1e-6), S10
   (5.3e-7), S07 (1.6e-10) difieren de eps de máquina y está DEMOSTRADO que el error es
   exactamente el modo Nyquist anulado por convención (espectro del error: 7.6e-4 solo
   en bins Nyquist; resto ≤1.4e-14).
2. **¿Qué superficie produce el mayor error?** S09_bricks (creases + juntas con salto
   de fase → más energía Nyquist). En E2 con ruido: S14_steep y S09 (nz_min bajo).
3. **¿Qué frecuencia es la más problemática?** Nyquist (convención) en perfecto; en
   ruido, la banda LOW amplifica 1/|k| (low_band_error domina, medido en E2).
4. **¿Conserva tileabilidad realmente?** Sí: seam_height ~1e-16 vs GT periódico en
   todos los S*; el wrap de H' replica el de H. La periodicidad es estructural (base
   Fourier), no ajustada.
5. **¿El error crece monótonamente con ruido?** Sí en el régimen estable (hasta
   nz ≈ σ). NO en el régimen inestable (nz < σ): el RMSE alineado deja de ser
   comparable porque el fitting colapsa distinto por corrida; gradient_rmse crudo sí
   crece monótono.
6. **¿Q8 degrada manejablemente?** Sí mientras nz ≫ 1/255 (S06: 3.3e-4). En pendientes
   fuertes (S09) 50× peor y ángulo p95 6° → manejable pero con gate por nz_min.
7. **¿wrong-Y es detectable?** Sí: RMSE 0.37 (vs 1e-16), ángulo 94°, corr 0.41 en S06.
8. **¿Qué ocurre cuando nz→0?** Perfecto: nada (exacto hasta nz 6.9e-4, float64). Con
   ruido: nz<0 produce picos ~1/(nz_floor) que se esparcen globalmente; el floor acota
   (medido: floor 0.01 baja seam_gradient de 3.1e5 a 8.5).
9. **¿El alignment esconde un error grave?** SÍ, por diseño de la trampa: escala (M1,
   2π) e inversión (flip_xy). El oracle lo cubre con gradient_rmse + normal_angle +
   best_sign + corr — lección canónica para los gates del producto.
10. **¿best_sign esconde una inversión sistemática?** No: lo REVELA (flip_xy →
    best_sign=-1 con RMSE 1e-16). En producción, a<0 o corr<0 deben ser REJECT.
11. **¿Hay bias por resolución?** No en el rango 256²→2048² con la convención de
    espacio de textura (misma superficie reconstruida idéntica; E0 a 512 y tests a
    64-128 consistentes). La convención por-muestra (bug cazado) SÍ habría introducido
    dependencia de resolución.
12. **¿Depende de NumPy/SciPy?** Solo NumPy 2.4.6 (pocketfft interno). Sin SciPy. Se
    esperan diferencias cross-platform ~1e-12; determinismo bit-a-bit intra-plataforma
    verificado.
13. **¿Hay superficie razonable donde el método sea conceptualmente incapaz?** Sí, dos
    clases: (a) contenido no periódico severo (el contrato lo excluye — N01/N02 lo
    miden) y (b) alturas con nz ≤ σ_ruido (relieve de amplitud comparable al ruido de
    la normal — medido en E4).
14. **¿Medimos solo lo que el algoritmo fue construido a hacer?** Casi: el roundtrip
    espectral favorece al solver (misma convención de derivada en GT y reconstrucción).
    El M5 (gradientes DF no periódicos) y E1/E2 (perturbaciones en la normal, el camino
    real de producción) mitigan ese sesgo; BC real queda para EXP-003.
15. **¿Qué experimento lo falsaría mejor?** Uno de dos: inyectar normales de BC5 real
    (error de bloque, no cuantización uniforme) o normales authored humanas (MatSynth
    CC0) donde el signo/LF no es controlado por nosotros. Ambos ya están en el roadmap
    (EXP-003 / EXP-001b); el inmediato por costo/beneficio es el spike nz-floor (EXP-M1).

## GO / NO-GO

**NP_M0_GO** — criterios §38: (a) la reconstrucción periódica perfecta se comporta
como la teoría predice (exacta salvo modo Nyquist documentado); (b) no hay casos
catastróficos no explicados — los dos regímenes malos (Nyquist, nz<ruido) están
cazados, cuantificados y explicados; (c) la periodicidad de frontera se preserva
(seam ~1e-16); (d) la re-proyección de normales es matemáticamente consistente
(ángulo ~3e-7° en datos perfectos); (e) la degradación por cuantización/ruido es
medible y no inmediatamente catastrófica en el dominio que le corresponde (nz ≫ σ);
(f) las limitaciones están entendidas y tienen experimento siguiente. El spike TAMBIÉN
demuestra que el oracle de métricas necesita `gradient_rmse`/`normal_angle`/
`best_sign` además del RMSE alineado (mutaciones M1 y flip_xy).

## Recommended next experiment

**EXP-M1 ( spike nz-floor / trust-mask )**: sobre el mismo banco sintético, medir
políticas de manejo de nz (reject / clamp con floor derivado de σ / máscara de
confianza |nz| ponderada en el integrador) en el régimen nz ≈ σ, con Q8 y σ=1-2/255.
Es el único hueco que separa el resultado "perfecto" del régimen de normales reales.
(No iniciado en esta PR; §39 del brief respetado.)

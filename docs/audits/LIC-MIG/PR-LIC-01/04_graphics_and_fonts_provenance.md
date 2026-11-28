# PR-LIC-01 — 04 · Procedencia de recursos gráficos y tipográficos (LIC01-D / LIC01-E)

**Evidencia cruda:** `evidence/graphics/c2pa_manifests.json`, `evidence/fonts/nicegui_fonts.json`,
`evidence/fonts/upstream_fonts.json`, `evidence/github/font-*/`.
**Herramientas:** lector C2PA oficial (`c2pa-python`), `fontTools`, `Pillow`, inspección binaria.

---

## 0. Límites de esta investigación (declarados antes de las conclusiones)

1. **El historial de incorporación es irrecuperable en este entorno**: el clon es *shallow*
   (`git rev-parse --is-shallow-repository` → `true`, un único commit). **No** se puede reconstruir
   `commit de incorporación` ni "historial de modificaciones" sin los respaldos de
   `I:\Skyclaw_Backups` (EVIDENCE_MISSING). No se inventa.
2. **La cadena de firma C2PA no pudo validarse**: el lector reporta
   `signingCredential.untrusted` ("signing certificate untrusted") y `signingCredential.ocsp.skipped`.
   Es decir: el manifiesto es **legible y estructuralmente válido**, pero la confianza criptográfica
   en el firmante **no queda establecida** en este entorno. No se presenta como "validado".
3. **Ninguna imagen se borró, reemplazó ni regeneró.** Todo el trabajo es lectura.

---

## 1. Inventario de recursos gráficos (los 3 efectivamente distribuidos)

Se distribuyen porque `sky_claw.spec` incluye `("sky_claw/app/gui/assets", "sky_claw/app/gui/assets")`
en `datas`. Los tres están referenciados por CSS/GUI (ver `sky_claw/app/gui/styles.css`).

| # | Ruta | SHA-256 | Formato real | Dimensiones | Metadatos |
|---|---|---|---|---|---|
| D1 | `sky_claw/app/gui/assets/alduin_menace_bg.jpg` | `9e59424828e3a6eb4c2041e0917372709b5ad1cac8dbc941aee3f308c37bea39` | **JPEG** (JFIF 1.01) | 1168 × 784 | **sin EXIF, sin XMP, sin C2PA** (0 ocurrencias de JUMBF) |
| D2 | `sky_claw/app/gui/assets/parchment.png` | `b97be60bd023d46750b7fcaa2a89bce4dabbb2b8b7ef8812e1bc0b3fafabfb87` | **JPEG con manifiesto C2PA**, extensión `.png` | 1024 × 1024 | C2PA v2 + firma Es256 de *Google Media Processing Services* (Google LLC) |
| D3 | `sky_claw/app/gui/assets/stone_bg.png` | `ece12abc26e8786664eaba37e664e3a68c33bb0a59667acd6be3357dec287adf` | **JPEG con manifiesto C2PA**, extensión `.png` | 1024 × 1024 | C2PA v2 + firma Es256 de *Google Media Processing Services* (Google LLC) |

### 1.1 Hallazgo material: dos de los tres recursos declaran generación por IA de **Google**, no del generador que el propietario recordaba

Manifiesto C2PA leído con el lector oficial (no por `strings`):

```
claim_generator_info : {"name": "Google C2PA Core Generator Library", "version": "889977083:891957646"}
assertions[c2pa.actions.v2]:
  - {"action": "c2pa.created", "description": "Created by Google Generative AI.",
     "digitalSourceType": "http://cv.iptc.org/newscodes/digitalsourcetype/trainedAlgorithmicMedia"}
  - {"action": "c2pa.edited",  "description": "Applied imperceptible SynthID watermark.",
     "digitalSourceType": "…/trainedAlgorithmicMedia"}
signature_info      : {"alg": "Es256", "common_name": "Google Media Processing Services",
                       "issuer": "Google LLC", "time": "2026-04-01T20:12:23+00:00"}   (D2)
                      {"alg": "Es256", "common_name": "Google Media Processing Services",
                       "issuer": "Google LLC", "time": "2026-04-01T20:12:49+00:00"}   (D3)
```

- D2: `urn:c2pa:ce8cbc69-7a7b-3047-efb7-467839ca7eba`
- D3: `urn:c2pa:ee1fbdde-57ac-3220-2922-46703f668e2b`

Ambos incluyen la afirmación de **marca de agua SynthID**, que es una técnica de Google.

**Consecuencia:** la declaración previa del propietario ("la imagen del dragón fue generada con
Grok") sólo es aplicable —en el mejor de los casos— a **D1**, que es el que **no** tiene ningún
metadato. Para D2 y D3 el manifiesto apunta a otro generador. Esto **no** es una acusación ni una
conclusión jurídica: es una **contradicción de evidencia** que el propietario debe resolver
(`EVIDENCE_CONFLICT`).

### 1.2 Las cinco preguntas que hay que mantener separadas

| Dimensión | D1 (dragón) | D2/D3 (pergamino, piedra) |
|---|---|---|
| **Procedencia técnica** | desconocida; sin huella técnica alguna | C2PA: Google Generative AI + SynthID |
| **Condiciones contractuales del generador** | `OWNER_ATTESTED`: Grok con cuenta propia; **falta fecha/versión de los términos** | términos de Google vigentes al 2026-04-01: **no acreditados** |
| **Titularidad** | declarada por el propietario | no declarada en el manifiesto |
| **Derechos sobre elementos de terceros representados** | la imagen evoca una franquicia de tercero: **no analizado** | abstracto/piedra: riesgo bajo, no analizado |
| **Permiso de redistribución** | **no acreditado** | **no acreditado** |

**C2PA no es autorización jurídica.** Un manifiesto que dice "creado por IA de Google" prueba
*procedencia técnica*, no licencia de redistribución. Y en este caso ni siquiera está
criptográficamente validado.

### 1.3 Defecto técnico adicional (menor, pero real)

D2 y D3 son **datos JPEG** servidos con extensión `.png`. Funciona porque el navegador olfatea el
tipo real, pero: (a) confunde cualquier inventario por extensión; (b) el clasificador del inventario
del EXE decide por extensión → un `.png` que es JPEG puede caer en una rama distinta de
clasificación. No se corrige aquí (prohibido tocar assets); se registra para PR-LIC-05.

---

## 2. Inventario de fuentes tipográficas

### 2.1 Fuentes propias de la GUI (12 WOFF2, 4 familias) — distribuidas en `datas`

Metadatos leídos del `name` table con `fontTools` (no de la documentación):

| Familia | Archivos | SHA-256 (prefijo) | `name` ID 5 (versión) | Copyright en el archivo | Licencia |
|---|---|---|---|---|---|
| Cinzel | `cinzel-8vIJ7…` (latin-ext), `cinzel-…79mT7` (latin) | `8fc2606f…`, `09941fb1…` | Version 2.000 | Copyright 2020 The Cinzel Project Authors | `name` ID 13: SIL OFL 1.1 |
| EB Garamond | 4 archivos (normal/italic × latin/latin-ext) | `3f8ca84f…`, `15754d0e…`, `ba958a19…`, `1a0cc9de…` | Version 1.001 | Copyright 2017 The EB Garamond Project Authors | OFL 1.1 |
| Noto Sans Runic | 3 archivos | `1df38d3d…`, `ac50f438…`, `d39e25e1…` | Version 2.002; ttfautohint 1.8.4.7 | Copyright 2022 The Noto Project Authors | OFL 1.1 |
| Spline Sans Mono | 2 archivos | `46b7dcaf…`, `0ca9a398…` | Version 1.004; ttfautohint 1.8.4.7 | Copyright 2022 The Spline Sans Mono Project Authors | OFL 1.1 |

`fsType` = **0** (instalación embebible permitida) en los 12 archivos: coherente con OFL.

**Correspondencia con upstream (verificación, no supuesto):**

| Familia | Texto OFL recuperado (ref fija) | Versión upstream comparada | Veredicto |
|---|---|---|---|
| Cinzel | `NDISCOVER/Cinzel` `master` @ `dd598495b0fb` | TTF variable `Cinzel[wght].ttf`: **Version 2.000**, mismo copyright 2020 | **coincide** → `SUPPORTED_BY_METADATA` |
| EB Garamond | `octaviopardo/EBGaramond12` `master` @ `106a4a6d3779` | Italic **1.001** (coincide), Regular **1.002** (el distribuido es **1.001**) | **EVIDENCE_CONFLICT**: el archivo histórico no se certifica con la release actual |
| Noto Sans Runic | `notofonts/runic` `main` @ `d5da626cd3bd` | el repo no publica binarios en el árbol (sólo `sources/`) | `SUPPORTED_BY_METADATA`: versión y copyright coinciden, artefacto de release no recuperado |
| Spline Sans Mono | `SorkinType/SplineSansMono` `main` @ `b167db03b7d7` | TTF Regular **1.004** | **coincide** → `SUPPORTED_BY_METADATA` |

Es exactamente el caso que la misión advierte: **no usar la versión upstream actual para certificar
un archivo histórico**. EB Garamond lo demuestra empíricamente.

### 2.2 Reserved Font Names (RFN) — análisis OFL uno por uno

| Familia | RFN declarado en el OFL del proyecto | ¿Afecta al archivo distribuido? |
|---|---|---|
| Cinzel | **"Cinzel Decorative"** (línea 2 del `OFL.txt`) | **No**: la familia distribuida es `Cinzel`, que no es el nombre reservado. El subset WOFF2 es una modificación permitida (OFL §3) y conserva un nombre **no** reservado |
| EB Garamond | ninguno | No |
| Noto Sans Runic | ninguno | No |
| Spline Sans Mono | ninguno | No |

Conclusión prudente: con los datos disponibles **no** se identifica infracción de RFN. Pero:
(a) los archivos distribuidos son **subsets WOFF2** producidos por el pipeline de Google Fonts, es
decir **Modified Versions** en el sentido de OFL §1; (b) OFL §5 exige que la Modified Version no
use el RFN —cumplido— y que se conserve el aviso de copyright —cumplido en el `name` table—; (c) el
texto OFL **completo no viaja** dentro de los WOFF2 ni en el repo. Por eso:
`attribution_required = SI`, `license_text_required = SI`.

### 2.3 Fuentes que llegan por dependencia (no elegidas por el proyecto)

Dentro del wheel de NiceGUI hay **16 WOFF2** con estas familias (extraídos y hasheados en
`evidence/fonts/nicegui_fonts.json`):

| Familia | Archivos | Copyright | Licencia |
|---|---|---|---|
| Material Icons / Outlined / Round / Sharp | 4 | Copyright 2018/2019 Google | Apache-2.0 |
| Material Symbols Outlined / Rounded / Sharp | 3 | Copyright 2025 Google LLC | Apache-2.0 |
| Roboto (varios subsets/pesos) | 9 | Copyright 2011 The Roboto Project Authors | Apache-2.0 |

**Corrección de un supuesto habitual:** estas fuentes **no** son "Google Fonts con OFL". Son
Apache-2.0 y exigen texto + avisos, y el wheel de NiceGUI **no** trae ninguno de los dos.

---

## 3. Estado por recurso (resumen para la matriz)

| Recurso | `evidence_status` | `compliance_status` | Fase |
|---|---|---|---|
| D1 `alduin_menace_bg.jpg` | `OWNER_ATTESTED` | `LEGAL_REVIEW_REQUIRED` | PR-LIC-05 |
| D2 `parchment.png` | `EVIDENCE_CONFLICT` | `LEGAL_REVIEW_REQUIRED` | PR-LIC-05 |
| D3 `stone_bg.png` | `EVIDENCE_CONFLICT` | `LEGAL_REVIEW_REQUIRED` | PR-LIC-05 |
| Cinzel | `SUPPORTED_BY_METADATA` | `OBLIGACION_REGISTRADA` | PR-LIC-04 |
| EB Garamond | `EVIDENCE_CONFLICT` | `OBLIGACION_REGISTRADA_VERSION_DESACTUALIZADA` | PR-LIC-04 |
| Noto Sans Runic | `SUPPORTED_BY_METADATA` | `OBLIGACION_REGISTRADA` | PR-LIC-04 |
| Spline Sans Mono | `SUPPORTED_BY_METADATA` | `OBLIGACION_REGISTRADA` | PR-LIC-04 |
| Material icons/symbols + Roboto (NiceGUI) | `SUPPORTED_BY_METADATA` | `OBLIGACION_REGISTRADA` | PR-LIC-04 |

---

## 4. Decisiones que requieren al propietario (no las toma este informe)

1. **Reemplazar o no** D1/D2/D3 por recursos con procedencia documentada y términos acreditados
   (prohibido tocarlos en esta fase).
2. Si mantiene D2/D3: aceptar por escrito el riesgo de redistribuir salida de IA de Google sin
   términos acreditados, o aportar la versión/fecha de los términos aplicables.
3. Si mantiene D1: aportar evidencia del generador (captura de la conversación, export del
   proveedor) o asumir la declaración como `OWNER_ATTESTED` en el expediente.
4. Si autoriza actualizar EB Garamond a 1.002 (implica cambio de asset y re-build).

# Third-Party Notices

Sky-Claw incorporates or redistributes third-party open-source software, fonts,
and frontend libraries. Original verbatim license texts, `NOTICE` files, and
machine-verifiable SHA-256 hashes are stored in `licenses/third_party/` and
bundled inside `SkyClawApp.exe` (exportable via `SkyClawApp.exe --export-licenses <DIR>`).

> **Alcance legal y técnico de este documento:** Este archivo y `licenses/third_party/`
> documentan las atribuciones y textos verbatim verificados hasta `PR-LIC-01`.
> **No constituye cumplimiento documental ni jurídico integral**: permanecen textos
> pendientes de verificación upstream en 9 activos frontend vendorizados de NiceGUI (`PR-LIC-04`),
> y **no satisface**
> de las obligaciones de disponibilidad de código fuente o reemplazo/relinking de
> bibliotecas `LGPL-2.1-or-later` (`py7zr`, `inflate64`, `multivolumefile`, `pybcj`, `pyppmd`)
> ni de `MPL-2.0 §3.2` (`bidict`, `certifi`, `grpcio`, `orjson`), las cuales se tratan
> explícitamente en **PR-LIC-02**, ni resuelve las revisiones jurídicas pendientes de
> `PR-LIC-04` (frontera MO2/PyQt6, `auto_parallax_regla.py`, recursos gráficos IA y
> Microsoft Windows Runtime).

> **Normalización de hashes en `manifest.json`:** Los hashes SHA-256 de `licenses/third_party/manifest.json`
> corresponden a **texto canónico normalizado a LF (`\n`)** para garantizar paridad entre checkouts Git
> de Windows y Linux. Son distintos de los hashes binarios originales de procedencia preservados en los
> respaldos de auditoría (`LIC-MIG-AUDIT`).

---

## 1. Hermes Agent (Conceptual & Protocol Attribution)

Sky-Claw includes design ideas and protocol-level inspiration from
[Hermes Agent](https://github.com/NousResearch/hermes-agent), developed by
Nous Research.

In particular, Sky-Claw's LLM tool-calling compatibility layer, including
XML `<tool_call>` handling (`sky_claw/app/agent/xml_tool_call_parser.py` and
the compatibility shim `sky_claw/app/agent/hermes_parser.py`), was informed by
conventions used by Hermes Agent. Sky-Claw is an independent implementation.
No Hermes source files are redistributed by this repository.

- Project: [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent)
- Copyright: © 2025 Nous Research
- License: [MIT](https://github.com/NousResearch/hermes-agent/blob/main/LICENSE)

Sky-Claw is not affiliated with, endorsed by, or maintained by Nous Research.

---

## 2. Bundled GUI Fonts (SIL Open Font License 1.1)

The 11 `.woff2` font files in `sky_claw/app/gui/assets/fonts/` remain licensed
under **SIL Open Font License, Version 1.1 (`OFL-1.1`)** and are **not**
relicensed under Sky-Claw's software license. Full license text is provided in
`sky_claw/app/gui/assets/fonts/OFL.txt` and `licenses/third_party/texts/`.

| Family | Files | Copyright | Reserved Font Name (RFN) | Upstream |
|---|---:|---|---|---|
| **Cinzel** | 2 | Copyright 2020 The Cinzel Project Authors | `"Cinzel Decorative"` (does not affect `Cinzel`) | https://github.com/NDISCOVER/Cinzel |
| **EB Garamond** | 4 | Copyright 2017 The EB Garamond Project Authors | None declared | https://github.com/octaviopardo/EBGaramond12 |
| **Noto Sans Runic** | 3 | Copyright 2022 The Noto Project Authors | None declared | https://github.com/notofonts/runic |
| **Spline Sans Mono** | 2 | Copyright 2022 The Spline Sans Mono Project Authors | None declared | https://github.com/SorkinType/SplineSansMono |

---

## 3. Native Runtime & Bootloader Components

| Component | Version | Concluded License | Notes |
|---|---|---|---|
| **PyInstaller Bootloader & Hooks** | `6.22.3` | `GPL-2.0-or-later WITH PyInstaller-Bootloader-Exception` (Bootloader Exception; runtime hooks: `Apache-2.0`) | See verbatim `COPYING.txt` in `licenses/third_party/texts/` |
| **CPython Runtime** | `3.11.9` | `PSF-2.0` | Copyright © 2001-2024 Python Software Foundation |
| **OpenSSL (`libcrypto-3.dll`, `libssl-3.dll`)** | `3.0.x` | `Apache-2.0` | Copyright © 1998-2024 The OpenSSL Project |
| **SQLite (`sqlite3.dll`)** | `3.x` | `Public-Domain` | Public Domain (blessing) |
| **libffi (`libffi-8.dll`)** | `3.4.x` | `MIT` | Copyright © 1996-2024 Anthony Green, Red Hat, Inc. and others |
| **Microsoft Windows Runtime (`VCRUNTIME140.dll`, `ucrtbase.dll`, `api-ms-win-*.dll`)** | Windows SDK / VS | `PROPRIETARY_REDISTRIBUTABLE` | Pending formal GPLv3 System Library Exception review (`PR-LIC-04`) |

---

## 4. Python Dependencies — Base Profile (87 redistributed packages)

Original verbatim license/NOTICE texts for every package are indexed by SHA-256 in
`licenses/third_party/manifest.json`.

| Package | Version | Concluded License | Source Obligation / Notes |
|---|---|---|---|
| `aiofiles` | `24.1.0` | `Apache-2.0` | NO |
| `aiohappyeyeballs` | `2.6.1` | `PSF-2.0` | NO |
| `aiohttp` | `3.14.4` | `Apache-2.0 AND MIT` | NO |
| `aiosignal` | `1.4.0` | `Apache-2.0` | NO |
| `aiosqlite` | `0.22.1` | `MIT` | NO |
| `annotated-doc` | `0.0.4` | `MIT` | NO |
| `annotated-types` | `0.7.0` | `MIT` | NO |
| `anyio` | `4.14.2` | `MIT` | NO |
| `attrs` | `26.1.0` | `MIT` | NO |
| `backports-tarfile` | `1.2.0` | `MIT` | NO |
| `backports-zstd` | `1.5.0` | `PSF-2.0 AND BSD-3-Clause` | NO |
| `bidict` | `0.23.1` | `MPL-2.0` | YES_MPL_2_0_SEC_3_2 (PR-LIC-02) |
| `brotli` | `1.2.1` | `MIT` | NO |
| `certifi` | `2026.10.19` | `MPL-2.0` | YES_MPL_2_0_SEC_3_2 (PR-LIC-02) |
| `cffi` | `2.0.0` | `MIT-0` | NO |
| `click` | `8.3.3` | `BSD-3-Clause` | NO |
| `colorama` | `0.4.6` | `BSD-3-Clause` | NO |
| `comtypes` | `1.4.16` | `MIT` | NO |
| `cryptography` | `50.0.0` | `Apache-2.0 OR BSD-3-Clause` | NO |
| `defusedxml` | `0.7.1` | `PSF-2.0` | NO |
| `docutils` | `0.22.4` | `Public-Domain AND BSD-2-Clause AND BSD-0-Clause AND PSF-2.0` | NO |
| `fastapi` | `0.136.1` | `MIT` | NO |
| `frozenlist` | `1.8.0` | `Apache-2.0` | NO |
| `googleapis-common-protos` | `1.75.0` | `Apache-2.0` | NO |
| `grpcio` | `1.80.0` | `Apache-2.0 AND BSD-3-Clause AND MPL-2.0` | YES_MPL_2_0_SEC_3_2 (PR-LIC-02) |
| `h11` | `0.16.0` | `MIT` | NO |
| `httpcore` | `1.0.9` | `BSD-3-Clause` | NO |
| `httptools` | `0.7.1` | `MIT` | NO |
| `httpx` | `0.28.1` | `BSD-3-Clause` | NO |
| `idna` | `3.15` | `BSD-3-Clause` | NO |
| `ifaddr` | `0.2.0` | `MIT` | NO |
| `importlib-metadata` | `8.7.1` | `Apache-2.0` | NO |
| `inflate64` | `1.0.4` | `LGPL-2.1-or-later` | YES_LGPL_2_1_SEC_6 (PR-LIC-02) |
| `itsdangerous` | `2.2.0` | `BSD-3-Clause` | NO |
| `jaraco-classes` | `3.4.0` | `MIT` | NO |
| `jaraco-context` | `6.1.2` | `MIT` | NO |
| `jaraco-functools` | `4.4.0` | `MIT` | NO |
| `jinja2` | `3.1.6` | `BSD-3-Clause` | NO |
| `keyring` | `25.7.0` | `MIT` | NO |
| `markdown2` | `2.5.5` | `MIT` | NO |
| `markupsafe` | `3.0.3` | `BSD-3-Clause` | NO |
| `more-itertools` | `11.0.1` | `MIT` | NO |
| `multidict` | `6.9.1` | `Apache-2.0` | NO |
| `multivolumefile` | `0.2.3` | `LGPL-2.1-or-later` | YES_LGPL_2_1_SEC_6 (PR-LIC-02) |
| `nicegui` | `3.12.0` | `MIT` | NO |
| `opentelemetry-api` | `1.45.2` | `Apache-2.0` | NO |
| `opentelemetry-exporter-otlp-common` | `0.66b2` | `Apache-2.0` | NO |
| `opentelemetry-exporter-otlp-proto-common` | `1.45.2` | `Apache-2.0` | NO |
| `opentelemetry-exporter-otlp-proto-grpc` | `1.45.2` | `Apache-2.0` | NO |
| `opentelemetry-proto` | `1.45.2` | `Apache-2.0` | NO |
| `opentelemetry-sdk` | `1.45.2` | `Apache-2.0` | NO |
| `opentelemetry-semantic-conventions` | `0.66b2` | `Apache-2.0` | NO |
| `orjson` | `3.11.9` | `MPL-2.0 AND (Apache-2.0 OR MIT)` | YES_MPL_2_0_SEC_3_2 (PR-LIC-02) |
| `pefile` | `2024.8.26` | `MIT` | NO |
| `prometheus-client` | `0.25.0` | `Apache-2.0 AND BSD-2-Clause` | NO |
| `propcache` | `0.4.1` | `Apache-2.0` | NO |
| `protobuf` | `6.33.6` | `BSD-3-Clause` | NO |
| `psutil` | `7.2.2` | `BSD-3-Clause` | NO |
| `py7zr` | `1.1.3` | `LGPL-2.1-or-later` | YES_LGPL_2_1_SEC_6 (PR-LIC-02) |
| `pybcj` | `1.0.7` | `LGPL-2.1-or-later` | YES_LGPL_2_1_SEC_6 (PR-LIC-02) |
| `pycparser` | `3.0` | `BSD-3-Clause` | NO |
| `pycryptodomex` | `3.23.0` | `Public-Domain AND BSD-2-Clause` | NO |
| `pydantic` | `2.12.5` | `MIT` | NO |
| `pydantic-core` | `2.41.5` | `MIT` | NO |
| `pygments` | `2.20.0` | `BSD-2-Clause` | NO |
| `pyppmd` | `1.3.1` | `LGPL-2.1-or-later` | YES_LGPL_2_1_SEC_6 (PR-LIC-02) |
| `python-dotenv` | `1.2.2` | `BSD-3-Clause` | NO |
| `python-engineio` | `4.13.3` | `MIT` | NO |
| `python-json-logger` | `3.3.0` | `BSD-2-Clause` | NO |
| `python-multipart` | `0.0.32` | `Apache-2.0` | NO |
| `python-socketio` | `5.16.3` | `MIT` | NO |
| `pywin32-ctypes` | `0.2.3` | `BSD-3-Clause` | NO |
| `pyyaml` | `6.0.3` | `MIT` | NO |
| `rarfile` | `4.2` | `ISC` | NO |
| `starlette` | `1.3.1` | `BSD-3-Clause` | NO |
| `tenacity` | `9.1.4` | `Apache-2.0` | NO |
| `tinycss2` | `1.5.1` | `BSD-3-Clause` | NO |
| `tomli-w` | `1.2.0` | `MIT` | NO |
| `typing-extensions` | `4.15.0` | `PSF-2.0` | NO |
| `typing-inspection` | `0.4.2` | `MIT` | NO |
| `uvicorn` | `0.46.0` | `BSD-3-Clause` | NO |
| `watchfiles` | `1.1.1` | `MIT` | NO |
| `webencodings` | `0.5.1` | `BSD-3-Clause` | NO |
| `websockets` | `16.0` | `BSD-3-Clause` | NO |
| `wsproto` | `1.3.2` | `MIT` | NO |
| `yarl` | `1.25.1` | `Apache-2.0` | NO |
| `zipp` | `3.23.1` | `MIT` | NO |

## 5. Python Dependencies — Optional `native-parallax` Profile (+2 packages)

| Package | Version | Concluded License | Notes |
|---|---|---|---|
| `numpy` | `2.4.6` | `BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 AND Apache-2.0` | Full aggregated verbatim LICENSE preserved in `licenses/third_party/texts/` |
| `pillow` | `13.0.0` | `MIT-CMU` | Full aggregated verbatim LICENSE preserved in `licenses/third_party/texts/` |

---

## 6. Vendored Frontend Libraries & Fonts (via `nicegui==3.12.0`, 18 entries)

Each vendored library/font below is registered individually in `licenses/third_party/manifest.json`.
Full verbatim upstream license texts are bundled for components with verified exact versions (`FULL_TEXT_VERIFIED`);
components without verifiable upstream text or version remain explicitly marked `PENDING_DOCUMENTARY_EVIDENCE` for `PR-LIC-04`
(a short JS/CSS banner is **not** treated as a full license text).

| Library | Version | Concluded License | Evidence Status | Document Coverage | Phase |
|---|---|---|---|---|---|
| quasar | `2.18.5` | `MIT` | `VERIFIED_PRIMARY_SOURCE` | `FULL_TEXT_VERIFIED` | `PR-LIC-01-IMPL` |
| vue | `3.5.22` | `MIT` | `VERIFIED_PRIMARY_SOURCE` | `FULL_TEXT_VERIFIED` | `PR-LIC-01-IMPL` |
| socket.io-client | `4.8.1` | `MIT` | `VERIFIED_PRIMARY_SOURCE` | `FULL_TEXT_VERIFIED` | `PR-LIC-01-IMPL` |
| immutable | `bundled` | `MIT` | `VERIFIED_EXACT_ARTIFACT` | `FULL_TEXT_VERIFIED` | `PR-LIC-01-IMPL` |
| DOMPurify (`dompurify`) | `3.4.0` | `Apache-2.0 OR MPL-2.0` | `SUPPORTED_BY_METADATA` | `FULL_TEXT_VERIFIED` | `PR-LIC-01-IMPL` |
| leaflet + leaflet.draw | `1.9.4 / 1.0.4` | `BSD-2-Clause AND MIT` | `VERIFIED_PRIMARY_SOURCE` | `FULL_TEXT_VERIFIED` | `PR-LIC-01-IMPL` |
| three.js | `r180` | `MIT` | `VERIFIED_PRIMARY_SOURCE` | `FULL_TEXT_VERIFIED` | `PR-LIC-01-IMPL` |
| sortablejs | `1.15.7` | `MIT` | `VERIFIED_PRIMARY_SOURCE` | `FULL_TEXT_VERIFIED` | `PR-LIC-01-IMPL` |
| xterm.js | `bundled` | `MIT` | `SUPPORTED_BY_METADATA` | `PENDING_DOCUMENTARY_EVIDENCE` | `PR-LIC-04` |
| tailwindcss | `4.1.13` | `NOASSERTION` | `INFERRED` | `PENDING_DOCUMENTARY_EVIDENCE` | `PR-LIC-04` |
| sass (dart-sass) | `unverified` | `NOASSERTION` | `EVIDENCE_MISSING` | `PENDING_DOCUMENTARY_EVIDENCE` | `PR-LIC-04` |
| unocss | `unverified` | `NOASSERTION` | `INFERRED` | `PENDING_DOCUMENTARY_EVIDENCE` | `PR-LIC-04` |
| mermaid | `unverified` | `NOASSERTION` | `INFERRED` | `PENDING_DOCUMENTARY_EVIDENCE` | `PR-LIC-04` |
| codemirror | `unverified` | `NOASSERTION` | `INFERRED` | `PENDING_DOCUMENTARY_EVIDENCE` | `PR-LIC-04` |
| apache-echarts | `unverified` | `NOASSERTION` | `INFERRED` | `PENDING_DOCUMENTARY_EVIDENCE` | `PR-LIC-04` |
| plotly.js | `unverified` | `NOASSERTION` | `INFERRED` | `PENDING_DOCUMENTARY_EVIDENCE` | `PR-LIC-04` |
| ag-grid-community | `unverified` | `NOASSERTION` | `INFERRED` | `PENDING_DOCUMENTARY_EVIDENCE` | `PR-LIC-04` |
| Roboto & Material Icons/Symbols (via NiceGUI) | `Material Icons 1.017 / Symbols 2.911 / Roboto 3.009` | `Apache-2.0` | `SUPPORTED_BY_METADATA` | `FULL_TEXT_VERIFIED` | `PR-LIC-01-IMPL` |

---

## 7. Retained Legal Reviews & Pending Phases

- **PR-LIC-02 (Copyleft Source & Relinking Mechanisms):** `LGPL-2.1-or-later` (`py7zr`, `inflate64`, `multivolumefile`, `pybcj`, `pyppmd`) and `MPL-2.0 §3.2` (`bidict`, `certifi`, `grpcio`, `orjson`).
- **Separate Clean-Room Mission:** `sky_claw/local/assets/auto_parallax_regla.py` (`LEGAL_REVIEW_REQUIRED`, upstream `doodlum/skyrim-auto-parallax` `GPL-3.0-only`).
- **PR-LIC-04 (Legal Gate & Boundaries):** `skyclaw-mo2-bridge-plugin` (in-process MO2/`mobase`/`PyQt6` boundary), GUI AI-generated images (`alduin_menace_bg.jpg`, `stone_bg.png`, `parchment.png`), and Microsoft Windows Runtime DLLs.

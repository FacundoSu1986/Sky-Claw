# Third-Party Notices

> **DRAFT — not yet applied to the repository.** Produced by PR-LIC-01 (attribution,
> third-party licences and provenance audit). Items marked ⚠ are **not** resolved and
> are tracked in `07_unresolved_legal_obligations.md`. Do not publish this notice as
> complete while any ⚠ remains.

Sky-Claw is distributed as a single frozen Windows executable (`SkyClawApp.exe`) built
with PyInstaller. The components below are effectively redistributed inside that
executable and/or shipped alongside it.

## 1. The project itself

Sky-Claw is licensed under the MIT License — Copyright (c) 2025 FacundoSu1986.
See `LICENSE` in the repository. `GPL_MIGRATION_HOLD` is active: the project licence has
**not** changed.

## 2. How to obtain the full licence texts

1. Inside the program: `SkyClawApp.exe --licenses` prints every text to stdout and can
   write them to a directory (planned in PR-LIC-04 ⚠).
2. In the repository: `licenses/third_party/texts/` (grouped by licence) and
   `licenses/third_party/component_texts/` (aggregated or component-specific texts).
3. Alongside the release artefact: `THIRD_PARTY_NOTICES.md` + `licenses/` ⚠ (pending
   owner decision on the release layout, PR-LIC-04).

## 3. Components grouped by licence text

Copyright lines are reproduced as they appear in the distributed artefact itself. Where
an artefact does not carry a copyright line, that is stated instead of invented.

### MIT

| Component | Version | Copyright notice |
|---|---|---|
| aiosqlite | 0.22.1 | Copyright (c) 2022 Amethyst Reese |
| annotated-doc | 0.0.4 | Copyright (c) 2025 Sebastián Ramírez |
| annotated-types | 0.7.0 | Copyright (c) 2022 the contributors |
| anyio | 4.14.2 | Copyright (c) 2018 Alex Grönholm |
| attrs | 26.1.0 | Copyright (c) 2015 Hynek Schlawack and the attrs contributors |
| backports-tarfile | 1.2.0 | ⚠ the artefact carries no copyright line |
| brotli | 1.2.1 | Copyright (c) 2009, 2010, 2013-2016 by the Brotli Authors. |
| comtypes | 1.4.16 | Copyright (c) 2006-2013, Thomas Heller. |
| fastapi | 0.136.1 | Copyright (c) 2018 Sebastián Ramírez |
| httptools | 0.7.1 | Copyright (c) 2015 MagicStack Inc.  http://magic.io |
| ifaddr | 0.2.0 | Copyright (c) 2014 Stefan C. Mueller |
| Immutable.js (vendorizado: immutable.es.js) | NO VERIFICADO | Copyright (c) 2014-present, Lee Byron and other contributors |
| jaraco-classes | 3.4.0 | ⚠ the artefact carries no copyright line |
| jaraco-context | 6.1.2 | Copyright (c) 2026 <copyright holders> |
| jaraco-functools | 4.4.0 | Copyright (c) 2025 <copyright holders> |
| keyring | 25.7.0 | Copyright (c) 2025 <copyright holders> |
| libffi (usado por ctypes) | 3.4.x (NO VERIFICADO) | Copyright (c) 1996-2014 Anthony Green, Red Hat, Inc and others |
| markdown2 | 2.5.5 | Copyright (c) 2012 Trent Mick |
| more-itertools | 11.0.1 | Copyright (c) 2012 Erik Rose |
| nicegui | 3.12.0 | Copyright (c) 2021 Zauberzeug GmbH |
| pefile | 2024.8.26 | Copyright (c) 2004-2024 Ero Carrera |
| pydantic | 2.12.5 | Copyright (c) 2017 to present Pydantic Services Inc. and individual contributors. |
| pydantic-core | 2.41.5 | Copyright (c) 2022 Samuel Colvin |
| python-engineio | 4.13.3 | Copyright (c) 2015 Miguel Grinberg |
| python-socketio | 5.16.3 | Copyright (c) 2015 Miguel Grinberg |
| pyyaml | 6.0.3 | Copyright (c) 2017-2021 Ingy döt Net |
| Quasar Framework (vendorizado: quasar.umd.js, quasar.*.css) | NO VERIFICADO | Copyright (c) 2015-present Razvan Stoenescu |
| Recursos y código propios (xEdit Pascal, shim C# de Synthesis, styles.css, security_policy | n/a | Copyright (c) 2025-2026 FacundoSu1986 |
| simple-websocket | 1.1.0 | Copyright (c) 2021 Miguel Grinberg |
| skyclaw_bridge (plugin Python de MO2, frontera in-process) | bundle embebido en SkyClawApp.exe | Copyright (c) 2025-2026 FacundoSu1986 |
| socket.io-client (vendorizado: socket.io.min.js) | NO VERIFICADO | Copyright (c) 2014-2024 Guillermo Rauch |
| Tailwind CSS browser build + UnoCSS y presets (vendorizados) | NO VERIFICADO | ⚠ the artefact carries no copyright line |
| texttable | 1.7.0 | Copyright (c) 2020 Gerome Fournier |
| tomli-w | 1.2.0 | Copyright (c) 2021 Taneli Hukkinen |
| typing-inspection | 0.4.2 | ⚠ the artefact carries no copyright line |
| Vue.js (vendorizado: vue.esm-browser.js y .prod.js) | NO VERIFICADO (banner no declara versión | Copyright (c) 2018-present Yuxi (Evan) You and Vue contributors |
| watchfiles | 1.1.1 | Copyright (c) 2017 to present Samuel Colvin |
| wsproto | 1.3.2 | Copyright (c) 2017 Benno Rice and contributors |
| zipp | 3.23.1 | Copyright (c) 2026 <copyright holders> |

### Apache-2.0

| Component | Version | Copyright notice |
|---|---|---|
| aiofiles | 24.1.0 | Copyright 2016 Tin Tvrtkovic |
| aiohttp | 3.14.4 | Copyright © 2018 Fedor Indutny |
| aiosignal | 1.4.0 | ⚠ the artefact carries no copyright line |
| cryptography | 50.0.0 | ⚠ the artefact carries no copyright line |
| DOMPurify (vendorizado en nicegui/static/dompurify.mjs) | 3.4.0 (según banner en el archivo) | Copyright (c) Cure53 and other contributors |
| frozenlist | 1.8.0 | ⚠ the artefact carries no copyright line |
| googleapis-common-protos | 1.75.0 | ⚠ the artefact carries no copyright line |
| grpcio | 1.80.0 | ⚠ the artefact carries no copyright line |
| importlib-metadata | 8.7.1 | ⚠ the artefact carries no copyright line |
| Material Icons / Material Symbols / Roboto (16 WOFF2 vendorizados dentro de NiceGUI) | Material Icons 1.017 / Material Icons Ou | Copyright 2018/2019/2025 Google LLC / Copyright 2011 The Roboto Project Authors |
| multidict | 6.9.1 | ⚠ the artefact carries no copyright line |
| OpenSSL (embebido por la distribución Windows de CPython) | 3.x (NO VERIFICADO) | Copyright 1998-2024 The OpenSSL Project Authors; Copyright (c) 1995-1998 Eric A. Young, Tim J. Hudson |
| opentelemetry-api | 1.45.2 | ⚠ the artefact carries no copyright line |
| opentelemetry-exporter-otlp-common | 0.66b2 | ⚠ the artefact carries no copyright line |
| opentelemetry-exporter-otlp-proto-common | 1.45.2 | ⚠ the artefact carries no copyright line |
| opentelemetry-exporter-otlp-proto-grpc | 1.45.2 | ⚠ the artefact carries no copyright line |
| opentelemetry-proto | 1.45.2 | ⚠ the artefact carries no copyright line |
| opentelemetry-sdk | 1.45.2 | ⚠ the artefact carries no copyright line |
| opentelemetry-semantic-conventions | 0.66b2 | ⚠ the artefact carries no copyright line |
| orjson | 3.11.9 | ⚠ the artefact carries no copyright line |
| prometheus-client | 0.25.0 | Copyright 2015 The Prometheus Authors |
| propcache | 0.4.1 | Copyright 2016-2021, Andrew Svetlov and aio-libs team |
| PyInstaller run-time hooks y módulos falsos (pyi_rth_*, _pyi_rth_utils) | 6.22.3 (+ pyinstaller-hooks-contrib 2026 | PyInstaller Development Team |
| python-multipart | 0.0.32 | ⚠ the artefact carries no copyright line |
| tenacity | 9.1.4 | ⚠ the artefact carries no copyright line |
| yarl | 1.25.1 | Copyright 2016-2021, Andrew Svetlov and aio-libs team |

### BSD-3-Clause

| Component | Version | Copyright notice |
|---|---|---|
| click | 8.3.3 | Copyright 2014 Pallets |
| colorama | 0.4.6 | Copyright (c) 2010 Jonathan Hartley |
| httpcore | 1.0.9 | Copyright © 2020, [Encode OSS Ltd](https://www.encode.io/). |
| httpx | 0.28.1 | Copyright © 2019, [Encode OSS Ltd](https://www.encode.io/). |
| idna | 3.15 | Copyright (c) 2013-2026, Kim Davies and contributors. |
| itsdangerous | 2.2.0 | Copyright 2011 Pallets |
| jinja2 | 3.1.6 | Copyright 2007 Pallets |
| lxml | 6.1.1 | Copyright (c) 2004 Infrae. All rights reserved. |
| lxml-html-clean | 0.4.5 | Copyright (c) 2004 Infrae. All rights reserved. |
| markupsafe | 3.0.3 | Copyright 2010 Pallets |
| protobuf | 6.33.6 | Copyright 2008 Google Inc.  All rights reserved. |
| psutil | 7.2.2 | Copyright (c) 2009, Jay Loden, Dave Daeschler, Giampaolo Rodola |
| pycparser | 3.0 | Copyright (c) 2008-2022, Eli Bendersky |
| python-dotenv | 1.2.2 | Copyright (c) 2014, Saurabh Kumar (python-dotenv), 2013, Ted Tieken (django-dotenv-rw), 2013, Jacob Kaplan-Moss (django-dotenv) |
| pywin32-ctypes | 0.2.3 | Copyright (c) 2014, Enthought, Inc. |
| starlette | 1.3.1 | Copyright © 2018, [Encode OSS Ltd](https://www.encode.io/). |
| tinycss2 | 1.5.1 | Copyright (c) 2013-2020, Simon Sapin and contributors. |
| uvicorn | 0.46.0 | Copyright © 2017-present, [Encode OSS Ltd](https://www.encode.io/). |
| websockets | 16.0 | ⚠ the artefact carries no copyright line |

### ⚠ UNRESOLVED

| Component | Version | Copyright notice |
|---|---|---|
| alduin_menace_bg.jpg (fondo del dashboard) | archivo único 1168x784 JPEG (extensión . | FacundoSu1986 (declarado por el propietario) |
| Dart Sass compilado (vendorizado: sass.dart.js) + runtime Dart | NO VERIFICADO | ⚠ the artefact carries no copyright line |
| docutils | 0.22.4 | ⚠ the artefact carries no copyright line |
| parchment.png (fondo pergamino) — ES UN JPEG con extensión .png | 1024x1024 JPEG | sin titular identificado en el manifiesto |
| stone_bg.png (fondo piedra) — ES UN JPEG con extensión .png | 1024x1024 JPEG | sin titular identificado en el manifiesto |
| Terceros embebidos en el build Windows de CPython (zlib, expat, bz2, lzma/xz, mpdecimal, l | NO VERIFICADO | Ver Doc/license.rst de CPython |
| webencodings | 0.5.1 | ⚠ the artefact carries no copyright line |

### LGPL-2.1-or-later

| Component | Version | Copyright notice |
|---|---|---|
| inflate64 | 1.0.4 | Copyright (C) 1991, 1999 Free Software Foundation, Inc. |
| multivolumefile | 0.2.3 | Copyright (C) 1991, 1999 Free Software Foundation, Inc. |
| py7zr | 1.1.3 | Copyright (C) 1991, 1999 Free Software Foundation, Inc. |
| pybcj | 1.0.7 | Copyright (C) 1991, 1999 Free Software Foundation, Inc. |
| pyppmd | 1.3.1 | Copyright (C) 1991, 1999 Free Software Foundation, Inc. |

### PSF-2.0

| Component | Version | Copyright notice |
|---|---|---|
| aiohappyeyeballs | 2.6.1 | ⚠ the artefact carries no copyright line |
| backports-zstd | 1.5.0 | ⚠ the artefact carries no copyright line |
| CPython (intérprete + stdlib congelados) | 3.11.x (parche exacto NO VERIFICADO) | Copyright (c) 2001-2026 Python Software Foundation; All Rights Reserved |
| defusedxml | 0.7.1 | ⚠ the artefact carries no copyright line |
| typing-extensions | 4.15.0 | ⚠ the artefact carries no copyright line |

### OFL-1.1

| Component | Version | Copyright notice |
|---|---|---|
| Cinzel (WOFF2, subsets latin/latin-ext) | Version 2.000 | Copyright 2020 The Cinzel Project Authors (https://github.com/NDISCOVER/Cinzel); OFL declara Reserved Font Name 'Cinzel Decorative' |
| EB Garamond (WOFF2, normal/italic × latin/latin-ext) | Version 1.001 (el upstream actual sirve  | Copyright 2017 The EB Garamond Project Authors (https://github.com/octaviopardo/EBGaramond12) |
| Noto Sans Runic (WOFF2, 3 subsets) | Version 2.002 | Copyright 2022 The Noto Project Authors (https://github.com/notofonts/runic) |
| Spline Sans Mono (WOFF2, latin/latin-ext) | Version 1.004 (coincide con el upstream  | Copyright 2022 The Spline Sans Mono Project Authors (https://github.com/SorkinType/SplineSansMono) |

### BSD-2-Clause

| Component | Version | Copyright notice |
|---|---|---|
| pygments | 2.20.0 | Copyright (c) 2006-2022 by the respective authors (see AUTHORS file). |
| python-json-logger | 3.3.0 | Copyright (c) 2011, Zakaria Zajac and the python-json-logger Contributors |

### MIT-0

| Component | Version | Copyright notice |
|---|---|---|
| cffi | 2.0.0 | ⚠ the artefact carries no copyright line |
| h11 | 0.16.0 | Copyright (c) 2016 Nathaniel J. Smith <njs@pobox.com> and other contributors |

### MPL-2.0

| Component | Version | Copyright notice |
|---|---|---|
| bidict | 0.23.1 | Copyright 2009-2024 Joshua Bronson. All rights reserved. |
| certifi | 2026.10.19 | ⚠ the artefact carries no copyright line |

### 0BSD

| Component | Version | Copyright notice |
|---|---|---|
| rarfile | 4.2 | Copyright (c) 2005-2024 Marko Kreen <markokr@gmail.com> |

### Dominio público

| Component | Version | Copyright notice |
|---|---|---|
| SQLite (módulo sqlite3 del stdlib) | 3.x (NO VERIFICADO) | SQLite no declara copyright; el autor renuncia a derechos |

### GPL-2.0-or-later WITH Bootloader-exception

| Component | Version | Copyright notice |
|---|---|---|
| PyInstaller bootloader + loader de runtime | 6.22.3 | Copyright (c) 2010-2024 PyInstaller Development Team |

### GPL-3.0-or-later

| Component | Version | Copyright notice |
|---|---|---|
| auto_parallax_regla.py (transcripción de la regla del plugin Auto Parallax) | archivo único; upstream citado: rama mai | doodlum y contribuidores de skyrim-auto-parallax |

### MIT-CMU

| Component | Version | Copyright notice |
|---|---|---|
| pillow | 13.0.0 | Copyright © 1997-2011 by Secret Labs AB |

### Unlicense

| Component | Version | Copyright notice |
|---|---|---|
| pycryptodomex | 3.23.0 | ⚠ the artefact carries no copyright line |

### compuesta: BSD-3-Clause

| Component | Version | Copyright notice |
|---|---|---|
| numpy | 2.4.6 | Copyright (c) 2005-2025, NumPy Developers. |

## 4. Special notices

### 4.1 PyInstaller

This executable contains the PyInstaller bootloader and runtime loader, licensed under
the GNU General Public License v2 **with the PyInstaller Bootloader Exception**, which
permits embedding them in a combined program without imposing the GPL on that program.
PyInstaller's run-time hooks and the `_pyi_rth_utils` / `pyi_splash` modules are
licensed under the Apache License 2.0. Full text: `GPL-2.0-or-later-WITH-Bootloader-
exception.txt` and `Apache-2.0.txt`.

### 4.2 Python itself

The frozen runtime is CPython, licensed under the Python Software Foundation License
Version 2, and includes third-party code (OpenSSL, SQLite, libffi, zlib, expat, bz2,
lzma, mpdecimal). Their notices are reproduced in full from CPython's own
`Doc/license.rst`. ⚠ The exact CPython/OpenSSL patch level of a given build cannot be
stated until the build environment manifest is recovered (PR-LIC-02).

### 4.3 Aggregated licence files

`numpy`, `pillow`, `grpcio` and `pycryptodomex` ship **aggregated** licence files covering
code from several upstream projects. Those files are reproduced in full and unedited in
`licenses/third_party/component_texts/`; summarising them would drop required notices.

### 4.4 Weak-copyleft components (LGPL-2.1-or-later, MPL-2.0)

The following components are under weak copyleft: `py7zr`, `pybcj`, `pyppmd`,
`inflate64`, `multivolumefile` (LGPL-2.1-or-later) and `bidict`, `certifi`, `grpcio`
partly, `orjson` partly (MPL-2.0).

**Including the licence text is not, by itself, compliance with those licences.** The
corresponding-source and relinking/replacement obligations are being engineered in
PR-LIC-02 and are **not** satisfied at the time of this draft. This notice will state
the mechanism once it exists.

### 4.5 Graphics

| Asset | SHA-256 (prefix) | Provenance status |
|---|---|---|
| sky_claw/app/gui/assets/alduin_menace_bg.jpg | `9e59424828e3a6eb…` | OWNER_ATTESTED ⚠ |
| sky_claw/app/gui/assets/parchment.png | `b97be60bd023d467…` | EVIDENCE_CONFLICT ⚠ |
| sky_claw/app/gui/assets/stone_bg.png | `ece12abc26e87866…` | EVIDENCE_CONFLICT ⚠ |

Two of the three images carry a C2PA manifest asserting AI generation by Google
(`trainedAlgorithmicMedia`, SynthID watermark) whose signer chain could not be validated
here; the third carries no metadata at all. Redistribution terms are **not** yet
evidenced — see PR-LIC-05 (owner decision).

## 5. Preserved prior attributions

### Hermes Agent (Nous Research)

Sky-Claw includes design ideas and protocol-level inspiration from [Hermes Agent]
(https://github.com/NousResearch/hermes-agent), developed by Nous Research. Sky-Claw's
LLM tool-calling compatibility layer, including XML `<tool_call>` handling, was informed
by conventions used by Hermes Agent. Sky-Claw is an independent implementation: no
Hermes source files are redistributed.

- Copyright: © 2025 Nous Research
- License: MIT

Sky-Claw is not affiliated with, endorsed by, or maintained by Nous Research.

## 6. Items explicitly NOT resolved by this notice

1. `auto_parallax_regla.py` is a transcription of a GPL-3.0 upstream plugin rule and is
   currently redistributed. Its legal status is under review (separate mission).
2. The MO2 in-process bridge plugin (`skyclaw_bridge`) runs inside Mod Organizer 2
   (GPL-3.0). Its classification as a separate work has not been established.
3. Texts still missing for: `webencodings`, the Apache-2.0 notices of NiceGUI's vendored
   web assets and icon fonts, DOMPurify/Vue/Quasar/socket.io/Immutable.js, Dart Sass.
4. `docutils` licensing is a documented mixed regime and has no single concluded licence.

## 7. Change log of this notice

| Date | Change |
|---|---|
| 2026-11-28 | Draft generated from the PR-LIC-01 effective-component matrix (124 rows). |


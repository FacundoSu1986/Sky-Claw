"""PR-LIC-01 — Generador del BORRADOR de THIRD_PARTY_NOTICES.md.

Toma la matriz (fuente de verdad de esta fase) y emite el documento 08. El objetivo del script no es
producir un archivo bonito: es demostrar que el aviso puede **generarse** de una fuente de verdad
verificable, de modo que no se desincronice a mano en cada PR.

Convenciones:
- El texto del borrador va en inglés porque el archivo vigente del repo está en inglés.
- Los huecos se marcan con ``⚠`` y remiten al documento 07; NO se inventan copyrights ni licencias.
"""

from __future__ import annotations

import argparse
import csv
from collections import defaultdict
from pathlib import Path

SECCIONES = [
    ("LIC01-A", "Python dependencies (redistributed in the frozen executable)"),
    ("LIC01-B", "Native runtime and vendored components"),
    ("LIC01-E", "Fonts"),
    ("LIC01-F", "Mod Organizer 2 integration boundary"),
    ("LIC01-G", "GPL-derived code pending legal review"),
]


def licencia_corta(texto: str) -> str:
    t = texto.strip()
    for marca in (" OR ", " AND ", " (", " + "):
        if marca in t:
            t = t.split(marca)[0]
    return t.strip() or "⚠ UNRESOLVED"


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--matriz", required=True)
    ap.add_argument("--salida", required=True)
    args = ap.parse_args(argv)

    rows = list(csv.DictReader(Path(args.matriz).open(encoding="utf-8")))
    distribuidas = [r for r in rows if not r["redistribution_scope"].startswith((
        "NO_REDISTRIBUIDO", "PRESENTE_EN_REPO", "NO_REDISTRIBUIDO_POR", "NO_ALCANZABLE_POR_IMPORT",
    ))]

    por_licencia: dict[str, list[dict[str, str]]] = defaultdict(list)
    for r in distribuidas:
        por_licencia[licencia_corta(r["license_concluded"])].append(r)

    out: list[str] = []
    out.append("# Third-Party Notices")
    out.append("")
    out.append("> **DRAFT — not yet applied to the repository.** Produced by PR-LIC-01 (attribution,")
    out.append("> third-party licences and provenance audit). Items marked ⚠ are **not** resolved and")
    out.append("> are tracked in `07_unresolved_legal_obligations.md`. Do not publish this notice as")
    out.append("> complete while any ⚠ remains.")
    out.append("")
    out.append("Sky-Claw is distributed as a single frozen Windows executable (`SkyClawApp.exe`) built")
    out.append("with PyInstaller. The components below are effectively redistributed inside that")
    out.append("executable and/or shipped alongside it.")
    out.append("")
    out.append("## 1. The project itself")
    out.append("")
    out.append("Sky-Claw is licensed under the MIT License — Copyright (c) 2025 FacundoSu1986.")
    out.append("See `LICENSE` in the repository. `GPL_MIGRATION_HOLD` is active: the project licence has")
    out.append("**not** changed.")
    out.append("")
    out.append("## 2. How to obtain the full licence texts")
    out.append("")
    out.append("1. Inside the program: `SkyClawApp.exe --licenses` prints every text to stdout and can")
    out.append("   write them to a directory (planned in PR-LIC-04 ⚠).")
    out.append("2. In the repository: `licenses/third_party/texts/` (grouped by licence) and")
    out.append("   `licenses/third_party/component_texts/` (aggregated or component-specific texts).")
    out.append("3. Alongside the release artefact: `THIRD_PARTY_NOTICES.md` + `licenses/` ⚠ (pending")
    out.append("   owner decision on the release layout, PR-LIC-04).")
    out.append("")
    out.append("## 3. Components grouped by licence text")
    out.append("")
    out.append("Copyright lines are reproduced as they appear in the distributed artefact itself. Where")
    out.append("an artefact does not carry a copyright line, that is stated instead of invented.")
    out.append("")
    for lic in sorted(por_licencia, key=lambda k: (-len(por_licencia[k]), k)):
        comps = sorted(por_licencia[lic], key=lambda r: r["component_name"].lower())
        out.append(f"### {lic}")
        out.append("")
        out.append("| Component | Version | Copyright notice |")
        out.append("|---|---|---|")
        for c in comps:
            cp = c["copyright"] or "⚠ not located in the artefact"
            if "NO LOCALIZADO" in cp:
                cp = "⚠ the artefact carries no copyright line"
            cp = cp.replace("|", "\\|")[:200]
            out.append(f"| {c['component_name'][:90]} | {c['exact_version'][:40]} | {cp} |")
        out.append("")

    out.append("## 4. Special notices")
    out.append("")
    out.append("### 4.1 PyInstaller")
    out.append("")
    out.append("This executable contains the PyInstaller bootloader and runtime loader, licensed under")
    out.append("the GNU General Public License v2 **with the PyInstaller Bootloader Exception**, which")
    out.append("permits embedding them in a combined program without imposing the GPL on that program.")
    out.append("PyInstaller's run-time hooks and the `_pyi_rth_utils` / `pyi_splash` modules are")
    out.append("licensed under the Apache License 2.0. Full text: `GPL-2.0-or-later-WITH-Bootloader-")
    out.append("exception.txt` and `Apache-2.0.txt`.")
    out.append("")
    out.append("### 4.2 Python itself")
    out.append("")
    out.append("The frozen runtime is CPython, licensed under the Python Software Foundation License")
    out.append("Version 2, and includes third-party code (OpenSSL, SQLite, libffi, zlib, expat, bz2,")
    out.append("lzma, mpdecimal). Their notices are reproduced in full from CPython's own")
    out.append("`Doc/license.rst`. ⚠ The exact CPython/OpenSSL patch level of a given build cannot be")
    out.append("stated until the build environment manifest is recovered (PR-LIC-02).")
    out.append("")
    out.append("### 4.3 Aggregated licence files")
    out.append("")
    out.append("`numpy`, `pillow`, `grpcio` and `pycryptodomex` ship **aggregated** licence files covering")
    out.append("code from several upstream projects. Those files are reproduced in full and unedited in")
    out.append("`licenses/third_party/component_texts/`; summarising them would drop required notices.")
    out.append("")
    out.append("### 4.4 Weak-copyleft components (LGPL-2.1-or-later, MPL-2.0)")
    out.append("")
    out.append("The following components are under weak copyleft: `py7zr`, `pybcj`, `pyppmd`,")
    out.append("`inflate64`, `multivolumefile` (LGPL-2.1-or-later) and `bidict`, `certifi`, `grpcio`")
    out.append("partly, `orjson` partly (MPL-2.0).")
    out.append("")
    out.append("**Including the licence text is not, by itself, compliance with those licences.** The")
    out.append("corresponding-source and relinking/replacement obligations are being engineered in")
    out.append("PR-LIC-02 and are **not** satisfied at the time of this draft. This notice will state")
    out.append("the mechanism once it exists.")
    out.append("")
    out.append("### 4.5 Graphics")
    out.append("")
    out.append("| Asset | SHA-256 (prefix) | Provenance status |")
    out.append("|---|---|---|")
    for r in rows:
        if r["component_id"].startswith("LIC01-D"):
            out.append(f"| {r['included_paths']} | `{r['artifact_sha256'][:16]}…` | {r['evidence_status']} ⚠ |")
    out.append("")
    out.append("Two of the three images carry a C2PA manifest asserting AI generation by Google")
    out.append("(`trainedAlgorithmicMedia`, SynthID watermark) whose signer chain could not be validated")
    out.append("here; the third carries no metadata at all. Redistribution terms are **not** yet")
    out.append("evidenced — see PR-LIC-05 (owner decision).")
    out.append("")
    out.append("## 5. Preserved prior attributions")
    out.append("")
    out.append("### Hermes Agent (Nous Research)")
    out.append("")
    out.append("Sky-Claw includes design ideas and protocol-level inspiration from [Hermes Agent]")
    out.append("(https://github.com/NousResearch/hermes-agent), developed by Nous Research. Sky-Claw's")
    out.append("LLM tool-calling compatibility layer, including XML `<tool_call>` handling, was informed")
    out.append("by conventions used by Hermes Agent. Sky-Claw is an independent implementation: no")
    out.append("Hermes source files are redistributed.")
    out.append("")
    out.append("- Copyright: © 2025 Nous Research")
    out.append("- License: MIT")
    out.append("")
    out.append("Sky-Claw is not affiliated with, endorsed by, or maintained by Nous Research.")
    out.append("")
    out.append("## 6. Items explicitly NOT resolved by this notice")
    out.append("")
    out.append("1. `auto_parallax_regla.py` is a transcription of a GPL-3.0 upstream plugin rule and is")
    out.append("   currently redistributed. Its legal status is under review (separate mission).")
    out.append("2. The MO2 in-process bridge plugin (`skyclaw_bridge`) runs inside Mod Organizer 2")
    out.append("   (GPL-3.0). Its classification as a separate work has not been established.")
    out.append("3. Texts still missing for: `webencodings`, the Apache-2.0 notices of NiceGUI's vendored")
    out.append("   web assets and icon fonts, DOMPurify/Vue/Quasar/socket.io/Immutable.js, Dart Sass.")
    out.append("4. `docutils` licensing is a documented mixed regime and has no single concluded licence.")
    out.append("")
    out.append("## 7. Change log of this notice")
    out.append("")
    out.append("| Date | Change |")
    out.append("|---|---|")
    out.append("| 2026-11-28 | Draft generated from the PR-LIC-01 effective-component matrix (124 rows). |")
    out.append("")

    Path(args.salida).write_text("\n".join(out) + "\n", encoding="utf-8")
    print(f"borrador generado: {args.salida} ({len(out)} líneas, {len(distribuidas)} componentes redistribuidos)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(__import__("sys").argv[1:]))

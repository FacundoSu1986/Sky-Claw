"""PR-LIC-01 — Consolidación del paquete de licencias y del registro de fuentes.

Produce:
- ``license_texts/<SPDX>.txt``: texto canónico de cada licencia concluida en la matriz (base SPDX
  license-list-data, ya descargada con SHA-256 registrado).
- ``license_texts/INDICE.md``: qué texto corresponde a qué componente y de dónde salió.
- ``evidence/registry/fuentes_consolidadas.md``: URLs + fecha de recuperación de TODA la evidencia.

Nota de método: para los componentes cuyo artefacto exacto trae su propio texto, el texto a
distribuir es el DEL ARTEFACTO (evidence/pypi/<pkg>/...). El texto canónico de SPDX se usa como
referencia estándar y como fuente cuando el artefacto NO incluye el texto (certifi, avisos Apache
de NiceGUI, licencias de fuentes, PyInstaller, CPython, OpenSSL...).
"""

from __future__ import annotations

import argparse
import csv
import json
import shutil
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

#: Licencia concluida (o familia) → archivo canónico en license-list-data.
MAPA_CANONICO = {
    "MIT": "MIT",
    "MIT-0": "MIT-0",
    "BSD-2-Clause": "BSD-2-Clause",
    "BSD-3-Clause": "BSD-3-Clause",
    "ISC": "ISC",
    "Apache-2.0": "Apache-2.0",
    "PSF-2.0": "PSF-2.0",
    "MPL-2.0": "MPL-2.0",
    "LGPL-2.1-or-later": "LGPL-2.1-or-later",
    "GPL-2.0-or-later": "GPL-2.0-or-later",
    "GPL-3.0-or-later": "GPL-3.0-or-later",
    "OFL-1.1": "OFL-1.1",
    "Zlib": "Zlib",
    "CC0-1.0": "CC0-1.0",
    "0BSD": "0BSD",
    "Unlicense": "Unlicense",
    "HPND": "HPND",
}


def familias(texto: str) -> set[str]:
    """Extrae familias SPDX de una cadena de licencia concluida (tolerante a prosa agregada)."""
    out: set[str] = set()
    if "WITH GCC-exception" in texto:
        texto = texto.replace("GPL-3.0-or-later WITH GCC-exception-3.1, solo aviso", "")
    for clave, destino in MAPA_CANONICO.items():
        if clave in texto:
            out.add(destino)
    for alias, destino in (("MIT-CMU", "HPND"), ("Dominio público", "Unlicense"), ("SQLite blessing", "Unlicense")):
        if alias in texto:
            out.add(destino)
    return out


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--matriz", required=True)
    ap.add_argument("--spdx-textos", required=True)
    ap.add_argument("--salida", required=True)
    ap.add_argument("--registro", required=True)
    args = ap.parse_args(argv)

    salida = Path(args.salida)
    salida.mkdir(parents=True, exist_ok=True)
    rows = list(csv.DictReader(Path(args.matriz).open(encoding="utf-8")))

    por_licencia: dict[str, list[str]] = defaultdict(list)
    for r in rows:
        if r["redistribution_scope"].startswith(("NO_REDISTRIBUIDO", "PRESENTE_EN_REPO", "NO_REDISTRIBUIDO_POR")):
            continue  # no se distribuye: no entra en el paquete de avisos
        for f in familias(r["license_concluded"]):
            por_licencia[f].append(f"{r['component_name']} {r['exact_version']}".strip())

    copiados = []
    for spdx in sorted(por_licencia):
        origen = Path(args.spdx_textos) / f"{spdx}.txt"
        if origen.exists():
            shutil.copyfile(origen, salida / f"{spdx}.txt")
            copiados.append(spdx)

    indice = ["# Índice de textos de licencia (PR-LIC-01)", ""]
    indice.append(f"Generado: {datetime.now(UTC).replace(microsecond=0).isoformat()}")
    indice.append("")
    indice.append("Base canónica: `spdx/license-list-data` (rama `main`, recuperada 2026-11-28,")
    indice.append("SHA-256 del tarball `084868b7c2be0019f3a7fa1460a1e71a93f1236ee6545e657843f00bd1c13b4d`).")
    indice.append("")
    indice.append("| Licencia | Componentes que la invocan | Texto en este directorio |")
    indice.append("|---|---|---|")
    for spdx in sorted(por_licencia):
        comps = por_licencia[spdx]
        muestra = ", ".join(sorted(comps)[:6]) + (f" (+{len(comps) - 6} más)" if len(comps) > 6 else "")
        estado = f"`{spdx}.txt`" if spdx in copiados else "**FALTA** (usar texto del artefacto exacto)"
        indice.append(f"| {spdx} | {muestra} | {estado} |")
    indice.append("")
    indice.append("## Regla de precedencia")
    indice.append("")
    indice.append("1. Si el artefacto exacto distribuido trae su propio texto, **se distribuye ese texto**")
    indice.append("   (`evidence/pypi/<paquete>/…`), no la versión canónica: es la prueba de lo que se")
    indice.append("   recibió y de lo que se redistribuye.")
    indice.append("2. Si el artefacto no trae texto (certifi, avisos Apache de los recursos web de NiceGUI,")
    indice.append("   fuentes, PyInstaller, CPython, OpenSSL), se usa el texto de la fuente primaria del")
    indice.append("   componente, con URL y fecha registradas en `evidence/registry/`.")
    indice.append("3. Ningún texto se transcribe de memoria.")
    (salida / "INDICE.md").write_text("\n".join(indice) + "\n", encoding="utf-8")

    # Registro consolidado de fuentes
    reg = Path(args.registro)
    lineas = ["# Registro de fuentes y fechas de recuperación", ""]
    lineas.append(f"Generado: {datetime.now(UTC).replace(microsecond=0).isoformat()}")
    lineas.append("")
    pypi = json.loads((Path(args.matriz).parent / "evidence" / "pypi" / "resumen.json").read_text(encoding="utf-8"))
    lineas.append(f"## PyPI (JSON oficial por versión exacta) — {len(pypi)} distribuciones")
    lineas.append("")
    lineas.append("| Distribución | Artefacto verificado | SHA-256 del artefacto | Hash en lock | Textos extraídos |")
    lineas.append("|---|---|---|---|---|")
    for p in pypi:
        art = (p.get("artifact") or {})
        textos = p.get("license_texts_extracted") or {}
        lineas.append(
            f"| {p.get('name')} {p.get('version')} | {art.get('filename', '—')} | `{(art.get('sha256_verified') or '')[:16]}…` | "
            f"{art.get('sha256_in_lock')} | {len(textos)} |"
        )
    gh = json.loads((Path(args.matriz).parent / "evidence" / "github" / "registro_github.json").read_text(encoding="utf-8"))
    lineas.append("")
    lineas.append(f"## GitHub API (ref fija) — {len(gh)} archivos")
    lineas.append("")
    lineas.append("| Componente | Ruta | Ref | Commit | SHA-256 | Fecha |")
    lineas.append("|---|---|---|---|---|---|")
    for g in gh:
        lineas.append(
            f"| {g['component_id']} | `{g['path']}` | {g['ref']} | `{(g.get('commit_resuelto') or '')[:12]}` | "
            f"`{(g.get('sha256') or g.get('error', ''))[:16]}` | {g['retrieved_at']} |"
        )
    reg.write_text("\n".join(lineas) + "\n", encoding="utf-8")

    print(f"licencias copiadas: {len(copiados)} → {salida}")
    print(f"registro: {reg}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(__import__("sys").argv[1:]))

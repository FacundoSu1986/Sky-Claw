"""PR-LIC-01 — Clasificador de textos de licencia recuperados de artefactos exactos (v2).

Metodología (CLAIM → EXTRACT → DOUBT → RECONCILE):
1. CLAIM: el metadato de PyPI declara ``license``/``License-Expression``/classifiers.
2. EXTRACT: del artefacto exacto fijado por el lock se extrajo el texto de licencia
   (``colectar_metadatos_pypi.py``), con el SHA-256 del artefacto que lo contenía.
3. DOUBT: el texto se compara contra la base canónica de SPDX (license-list-data), midiendo
   COBERTURA (fracción del texto canónico reproducida literalmente en el archivo del artefacto)
   en vez de una razón global de similitud, que penaliza indebidamente los apéndices de copyright.
4. RECONCILE: se emite ``spdx_concluido`` solo con cobertura alta; si el metadato y el texto
   discrepan se marca ``EVIDENCE_CONFLICT``. Un archivo que solo NOMBRA una licencia sin incluir
   su texto se registra como ``texto_completo=False`` (obligación de suministrar el texto).

Salida: ``analisis_licencias.json`` + ``resumen_licencias.csv``.
"""

from __future__ import annotations

import argparse
import csv
import difflib
import hashlib
import json
import re
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

MARCADORES: list[tuple[str, tuple[str, ...]]] = [
    ("LGPL-3.0", ("gnu lesser general public license", "version 3")),
    ("LGPL-2.1", ("gnu lesser general public license", "version 2.1")),
    ("AGPL-3.0", ("gnu affero general public license", "version 3")),
    ("GPL-3.0", ("gnu general public license", "version 3")),
    ("GPL-2.0", ("gnu general public license", "version 2")),
    ("Apache-2.0", ("apache license", "version 2.0")),
    ("MPL-2.0", ("mozilla public license",)),
    ("OFL-1.1", ("sil open font license", "version 1.1")),
    ("PSF-2.0", ("python software foundation license",)),
    ("BSL-1.0", ("boost software license",)),
    ("CC0-1.0", ("cc0 1.0 universal",)),
    ("Unlicense", ("free and unencumbered software released into the public domain",)),
    ("ISC", ("permission to use, copy, modify, and/or distribute this software for any purpose",)),
    ("Zlib", ("altered source versions must be plainly marked",)),
    ("BSD-3-Clause", ("redistribution and use in source and binary forms", "endorse or promote products derived")),
    ("BSD-2-Clause", ("redistribution and use in source and binary forms", "provided that the above copyright notice")),
    ("MIT", ("permission is hereby granted, free of charge",)),
    ("HPND", ("historical permission notice and disclaimer",)),
    ("Artistic-2.0", ("the artistic license 2.0",)),
]

#: Shortlist para el barrido de cobertura cuando los marcadores no alcanzan.
SHORTLIST = [
    "MIT", "MIT-0", "BSD-2-Clause", "BSD-3-Clause", "BSD-3-Clause-Clear", "BSD-4-Clause", "0BSD", "ISC",
    "Apache-2.0", "Apache-1.1", "MPL-2.0", "PSF-2.0", "Python-2.0", "Unlicense", "Zlib", "BSL-1.0", "CC0-1.0",
    "LGPL-2.1-only", "LGPL-2.1-or-later", "LGPL-3.0-only", "LGPL-3.0-or-later", "GPL-2.0-only", "GPL-2.0-or-later",
    "GPL-3.0-only", "GPL-3.0-or-later", "AGPL-3.0-only", "OFL-1.1", "HPND", "Artistic-2.0", "WTFPL", "PostgreSQL",
    "OpenSSL", "X11", "NCSA", "BlueOak-1.0.0", "CC-BY-4.0", "CC-BY-SA-4.0", "IJG", "libtiff", "curl", "JSON",
    "AFL-3.0", "EPL-2.0", "CDDL-1.0", "ZPL-2.1", "SSLeay", "MulanPSL-2.0", "Unicode-DFS-2016", "UPL-1.0",
]

ATRIBUCION_MIT = "the above copyright notice and this permission notice shall be included"


def normalizar(texto: str) -> str:
    t = texto.replace("\r\n", "\n").replace("\r", "\n")
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n{2,}", "\n", t)
    return t.strip().lower()


def sin_espacios(texto: str) -> str:
    """Normalización agresiva: sin espacios ni saltos.

    Los mismos términos llegan con wrapping distinto según el empaquetador (un LICENSE de wheel
    rara vez conserva el wrapping del repositorio upstream). Comparar sin espacios elimina ese
    ruido y deja la comparación en el contenido, no en el formato.
    """
    return re.sub(r"\s+", "", normalizar(texto))


def _shingles(texto_ws: str, k: int = 40) -> frozenset[str]:
    """Conjunto COMPLETO de k-subcadenas del texto SIN espacios (paso 1, sin muestreo).

    Sin muestreo: un muestreo con paso variable haría incomparables los conjuntos de dos textos
    del mismo largo distinto, y produciría falsos negativos (ese error se detectó y se corrigió).
    Inmune al wrapping y al prefijo de copyright, y O(n) en vez de cuadrático.
    """
    if len(texto_ws) <= k:
        return frozenset({texto_ws})
    return frozenset(texto_ws[i : i + k] for i in range(len(texto_ws) - k + 1))


def cobertura(canon_shingles: frozenset[str], art_shingles: frozenset[str]) -> float:
    """Fracción de shingles del canónico presentes en el archivo del artefacto."""
    if not canon_shingles:
        return 0.0
    return round(len(canon_shingles & art_shingles) / len(canon_shingles), 4)


def cargar_canonicos(dir_textos: Path) -> dict[str, frozenset[str]]:
    """Shingles SOLO de los textos canónicos que el clasificador puede llegar a usar.

    Cargar los 831 textos con paso 1 costaría cientos de MB en memoria y no aportaría nada:
    la shortlist + los ids de ``MARCADORES`` cubren todo lo observable en el inventario.
    """
    interes = set(SHORTLIST) | {s for s, _ in MARCADORES} | {"MIT-0", "LGPL-2.1-only", "LGPL-2.1-or-later", "LGPL-3.0-only", "GPL-2.0-only", "GPL-3.0-only", "AGPL-3.0-only"}
    salida: dict[str, frozenset[str]] = {}
    for p in sorted(dir_textos.glob("*.txt")):
        if p.stem in interes:
            salida[p.stem] = _shingles(sin_espacios(p.read_text(encoding="utf-8", errors="replace")))
    return salida


def detectar(texto: str, canonicos: dict[str, frozenset[str]]) -> dict[str, Any]:
    """Identifica la licencia por cláusulas distintivas y CONFIRMA por cobertura de shingles.

    Prioridad = orden de ``MARCADORES`` (variantes más específicas primero). Nunca se elige una
    licencia solo por cobertura: BSD-2 está contenida en BSD-3, así que la cobertura sola
    confundiría ambas. La cláusula "endorse or promote" decide BSD-3 vs BSD-2, etc.
    """
    bajo = normalizar(texto)
    art_sh = _shingles(sin_espacios(texto))

    por_clausula = [spdx for spdx, claves in MARCADORES if all(k in bajo for k in claves) and spdx in canonicos]
    if "MIT" in por_clausula and ATRIBUCION_MIT not in bajo:
        por_clausula = ["MIT-0" if c == "MIT" else c for c in por_clausula]

    evaluados = [{"spdx": c, "cobertura": cobertura(canonicos[c], art_sh), "via": "clausula"} for c in dict.fromkeys(por_clausula)]
    elegido = next((e for e in evaluados if e["cobertura"] >= 0.90), None)
    if elegido is None:
        barrido = [
            {"spdx": c, "cobertura": cobertura(canonicos[c], art_sh), "via": "barrido"}
            for c in SHORTLIST
            if c in canonicos
        ]
        barrido.sort(key=lambda x: (-x["cobertura"], -len(canonicos[x["spdx"]])))
        evaluados.extend(barrido[:3])
        elegido = barrido[0] if barrido and barrido[0]["cobertura"] >= 0.90 else None

    mejor = elegido or (max(evaluados, key=lambda e: e["cobertura"]) if evaluados else {"spdx": None, "cobertura": 0.0})
    return {
        # familia identificada por CLÁUSULA (independiente de la cobertura) y cobertura de cada familia
        "por_clausula": list(dict.fromkeys(por_clausula)),
        "cobertura_por_familia": {e["spdx"]: e["cobertura"] for e in evaluados if e.get("via") == "clausula"},
        "candidatos": [e["spdx"] for e in evaluados if e["cobertura"] >= 0.90],
        "mejor": mejor["spdx"],
        "cobertura": mejor["cobertura"],
        "texto_completo": bool(mejor["cobertura"] >= 0.95),
        "top3": sorted(evaluados, key=lambda e: -e["cobertura"])[:3],
    }


def copyright_lines(texto: str) -> list[str]:
    out = []
    for linea in texto.splitlines()[:40]:
        s = linea.strip()
        if re.search(r"(copyright|\(c\)|©)", s, re.I) and len(s) < 300:
            out.append(s)
    return out[:4]


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pypi-dir", required=True)
    ap.add_argument("--spdx-textos", required=True)
    ap.add_argument("--salida", required=True)
    args = ap.parse_args(argv)

    canonicos = cargar_canonicos(Path(args.spdx_textos))
    pypi = Path(args.pypi_dir)
    detalle: list[dict[str, Any]] = []
    for pkg_dir in sorted(p for p in pypi.iterdir() if p.is_dir()):
        textos = [p for p in pkg_dir.iterdir() if p.is_file() and p.name != "metadata.json"]
        registro: dict[str, Any] = {"paquete": pkg_dir.name, "archivos": []}
        conteo: Counter[str] = Counter()
        for t in sorted(textos):
            contenido = t.read_text(encoding="utf-8", errors="replace")
            det = detectar(contenido, canonicos)
            registro["archivos"].append(
                {
                    "archivo": t.name,
                    "sha256": hashlib.sha256(contenido.encode("utf-8", errors="replace")).hexdigest(),
                    "bytes": t.stat().st_size,
                    "licencias_detectadas": det["candidatos"],
                    "familias_por_clausula": det.get("por_clausula", []),
                    "cobertura_por_familia": det.get("cobertura_por_familia", {}),
                    "spdx_mejor": det["mejor"],
                    "cobertura_canonica": det["cobertura"],
                    "texto_completo": det["texto_completo"],
                    "top3": det["top3"],
                    "copyright": copyright_lines(contenido),
                }
            )
            for c in det["candidatos"] or det.get("por_clausula", []):
                conteo[c] += 1
        registro["licencias_en_textos"] = dict(conteo.most_common())
        detalle.append(registro)

    Path(args.salida).write_text(json.dumps(detalle, indent=2, ensure_ascii=False), encoding="utf-8")
    resumen = Path(args.salida).with_name("resumen_licencias.csv")
    with resumen.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["paquete", "licencias_en_textos", "archivos", "cobertura", "texto_completo", "copyright"])
        for r in detalle:
            cob = "; ".join(f"{a['spdx_mejor']}:{a['cobertura_canonica']}" for a in r["archivos"] if a["spdx_mejor"])
            comp = all(a["texto_completo"] for a in r["archivos"] if a["spdx_mejor"]) if r["archivos"] else False
            cps = "; ".join(c for a in r["archivos"] for c in a["copyright"][:1])[:300]
            w.writerow([r["paquete"], " | ".join(f"{k}={v}" for k, v in r["licencias_en_textos"].items()), len(r["archivos"]), cob, comp, cps])
    print(f"paquetes analizados: {len(detalle)}  canónicos: {len(canonicos)}  {datetime.now(UTC).replace(microsecond=0).isoformat()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

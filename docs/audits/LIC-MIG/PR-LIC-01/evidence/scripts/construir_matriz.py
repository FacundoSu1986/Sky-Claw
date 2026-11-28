"""PR-LIC-01 — Construcción de la matriz de componentes efectivos y sus obligaciones.

Fuentes (todas dentro de ``evidence/``):
- ``redistribucion.json``      → perfiles, alcanzabilidad desde ``__main__``/hiddenimports.
- ``pypi/analisis_licencias.json`` → familias identificadas y cobertura contra texto canónico SPDX.
- ``pypi/<pkg>/metadata.json``  → ``licenseDeclared`` (License-Expression / License / classifiers) — NUNCA concluida.
- ``pypi/<pkg>/*LICENSE*``      → texto del artefacto exacto (con SHA-256 del artefacto que lo contenía).
- ``github/registro_github.json``→ textos primarios de componentes no-PyPI (versión/commit fijos).

Estados de evidencia (separados del estado de cumplimiento, por diseño):
VERIFIED_PRIMARY_SOURCE · VERIFIED_EXACT_ARTIFACT · SUPPORTED_BY_METADATA · OWNER_ATTESTED ·
INFERRED · EVIDENCE_MISSING · EVIDENCE_CONFLICT · LEGAL_REVIEW_REQUIRED · REMEDIATION_REQUIRED · NOT_APPLICABLE

Salida: ``02_effective_component_license_matrix.csv`` + ``matriz_resumen.json``.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from pathlib import Path
from typing import Any

CAMPOS = [
    "component_id",
    "component_name",
    "exact_version",
    "component_type",
    "redistribution_scope",
    "build_profiles",
    "included_paths",
    "artifact_sha256",
    "license_declared",
    "license_concluded",
    "copyright",
    "license_text_source",
    "upstream_reference",
    "evidence_sha256",
    "attribution_required",
    "license_text_required",
    "notice_required",
    "source_obligation",
    "additional_obligations",
    "evidence_status",
    "compliance_status",
    "remediation_phase",
    "unresolved_questions",
]

#: Familias que exigen atribución y texto de licencia al redistribuir en forma binaria.
EXIGE_ATRIBUCION = {
    "MIT", "MIT-0", "BSD-2-Clause", "BSD-3-Clause", "BSD-3-Clause-Clear", "ISC", "Apache-2.0", "Apache-1.1",
    "PSF-2.0", "Python-2.0", "MPL-2.0", "LGPL-2.1-only", "LGPL-2.1-or-later", "LGPL-3.0-only",
    "LGPL-3.0-or-later", "GPL-2.0-only", "GPL-2.0-or-later", "GPL-3.0-only", "GPL-3.0-or-later",
    "OFL-1.1", "Zlib", "BSL-1.0", "Artistic-2.0",
}
SIN_OBLIGACION = {"Unlicense", "CC0-1.0", "0BSD", "MIT-0"}
COPyleft_FUERTE = {"GPL-2.0-only", "GPL-2.0-or-later", "GPL-3.0-only", "GPL-3.0-or-later", "AGPL-3.0-only"}
COPyleft_DEBIL = {"LGPL-2.1-only", "LGPL-2.1-or-later", "LGPL-3.0-only", "LGPL-3.0-or-later", "MPL-2.0"}


def _declarada(info: dict[str, Any]) -> str:
    if info.get("license_expression"):
        return str(info["license_expression"]).strip()
    lic = (info.get("license") or "").strip()
    if lic:
        return lic[:120]
    cls = [c.split("::")[-1].strip() for c in info.get("classifiers", []) if c.startswith("License ::")]
    return "; ".join(dict.fromkeys(cls)) or "NOASSERTION"


def _pkgs(pypi: Path) -> dict[str, dict[str, Any]]:
    out = {}
    for d in sorted(p for p in pypi.iterdir() if p.is_dir()):
        meta = d / "metadata.json"
        if not meta.exists():
            continue
        data = json.loads(meta.read_text(encoding="utf-8"))
        out[d.name] = data
    return out


def fila_python(
    dist: str,
    version: str,
    fila_red: dict[str, Any],
    analisis: dict[str, Any],
    meta: dict[str, Any],
) -> dict[str, str]:
    pkg_dir = f"{dist}-{version}"
    registro = meta["registro"]
    info = meta["pypi_json"]["info"]

    familias: dict[str, int] = analisis.get("licencias_en_textos", {}) or {}
    archivos = analisis.get("archivos", []) or []
    # familia concluida = la que aparece en el texto, con cobertura suficiente
    cand = [(f, n) for f, n in familias.items()]
    concluida = ""
    cob_txt = []
    for f, _n in cand:
        cobs = [a["cobertura_canonica"] for a in archivos if a.get("spdx_mejor") == f]
        if cobs:
            cob_txt.append(f"{f}:{max(cobs)}")
            if max(cobs) >= 0.85:
                concluida = f if not concluida else f"{concluida} OR {f}"
    if not familias:
        # sin marcador: se acepta la mejor familia del barrido solo si la cobertura es alta
        for a in archivos:
            if a.get("spdx_mejor") and a.get("cobertura_canonica", 0) >= 0.85:
                concluida = a["spdx_mejor"]
                cob_txt.append(f"{a['spdx_mejor']}:{a['cobertura_canonica']} (barrido)")
                break

    copyright_ = ""
    for a in archivos:
        for linea in a.get("copyright") or []:
            if re.search(r"(copyright|©|\(c\))\s*(\(c\)|©)?\s*\d{4}", linea, re.I):
                copyright_ = linea[:200]
                break
        if copyright_:
            break
    if not copyright_:
        copyright_ = "NO LOCALIZADO EN EL TEXTO DEL ARTEFACTO"

    textos = registro.get("license_texts_extracted") or {}
    artefacto = registro.get("artifact") or {}
    sha_art = next((v.get("artifact_sha256") for v in textos.values() if v.get("artifact_sha256")), "") or (
        artefacto.get("sha256_verified") or ""
    )
    nombres = ", ".join(sorted({a["archivo"] for a in archivos})) or "—"

    fam_set = {f.strip() for f in re.split(r" OR | AND ", concluida) if f.strip()}
    atrib = "SI" if fam_set & EXIGE_ATRIBUCION else ("NO" if fam_set and fam_set <= SIN_OBLIGACION else "INDETERMINADO")
    texto_lic = "SI" if fam_set & EXIGE_ATRIBUCION else ("NO" if fam_set and fam_set <= SIN_OBLIGACION else "INDETERMINADO")
    notice = "SI" if "Apache-2.0" in fam_set else "NO"
    fuente_obl = "SI" if fam_set & (COPyleft_DEBIL | COPyleft_FUERTE) else "NO"

    extra = []
    if not archivos:
        extra.append("el artefacto no contiene archivo de licencia: el texto debe obtenerse del upstream y verificarse")
    if any(not a.get("texto_completo") for a in archivos) and archivos:
        extra.append("texto del artefacto no idéntico al canónico SPDX (variante de redacción): se distribuye el texto del artefacto")
    if fam_set & COPyleft_DEBIL:
        extra.append("copyleft débil: obligaciones de código fuente/relinking evaluadas en PR-LIC-02, no resueltas aquí")

    if not fila_red["alcanzable_transitivo"]:
        alcance = "NO_ALCANZABLE_POR_IMPORT (¿build-only o import dinámico?)"
        evidencia = "INFERRED"
        cumplimiento = "REQUIERE_VERIFICACION_CON_ANALYSIS_TOC"
        fase = "PR-LIC-02"
        preguntas = "¿lo incorpora un hook de PyInstaller o un import dinámico no declarado?"
    elif fila_red["import_directo_o_hidden"]:
        alcance = "REDISTRIBUIDO_EN_EXE"
        evidencia = "VERIFIED_EXACT_ARTIFACT" if archivos else "SUPPORTED_BY_METADATA"
        cumplimiento = "OBLIGACION_REGISTRADA" if fam_set else "LEGAL_REVIEW_REQUIRED"
        fase = "PR-LIC-04" if fuente_obl == "NO" else "PR-LIC-02"
        preguntas = ""
    else:
        alcance = "REDISTRIBUIDO_EN_EXE (dependencia transitiva alcanzable)"
        evidencia = "VERIFIED_EXACT_ARTIFACT" if archivos else "SUPPORTED_BY_METADATA"
        cumplimiento = "OBLIGACION_REGISTRADA" if fam_set else "LEGAL_REVIEW_REQUIRED"
        fase = "PR-LIC-04" if fuente_obl == "NO" else "PR-LIC-02"
        preguntas = ""

    if not archivos:
        cumplimiento = "LEGAL_REVIEW_REQUIRED"
        preguntas = (preguntas + "; " if preguntas else "") + "falta el texto de licencia del artefacto exacto"

    return {
        "component_id": f"LIC01-A::{dist.lower()}",
        "component_name": dist,
        "exact_version": version,
        "component_type": "python_distribution",
        "redistribution_scope": alcance,
        "build_profiles": "base" if fila_red["lock_base"] else "", 
        "included_paths": "paquete importado por PyInstaller (Analysis TOC no disponible en esta fase)",
        "artifact_sha256": sha_art,
        "license_declared": _declarada(info),
        "license_concluded": concluida or "",
        "copyright": copyright_,
        "license_text_source": (
            f"artefacto exacto {artefacto.get('filename','?')} ({nombres}) — evidence/pypi/{pkg_dir}/"
            if archivos
            else "NO DISPONIBLE EN EL ARTEFACTO"
        ),
        "upstream_reference": f"https://pypi.org/project/{dist}/{version}/",
        "evidence_sha256": "; ".join(f"{a['archivo']}={a['sha256']}" for a in archivos) if archivos else "",
        "attribution_required": atrib,
        "license_text_required": texto_lic,
        "notice_required": notice,
        "source_obligation": fuente_obl,
        "additional_obligations": "; ".join(extra) or "ninguna adicional identificada",
        "evidence_status": evidencia,
        "compliance_status": cumplimiento,
        "remediation_phase": fase,
        "unresolved_questions": preguntas,
    }


def perfiles(fila: dict[str, Any], version_por_perfil: dict[str, list[str]]) -> str:
    p = []
    if fila.get("lock_base"):
        p.append("base")
    if fila.get("lock_native_parallax"):
        p.append("native-parallax")
    return "+".join(p)


#: Curación por componente: sólo donde el automatismo NO puede decidir (licencia agregada de varios
#: terceros, elección dual, texto ausente o NOTICE que declara un componente embebido).
OVERRIDES: dict[str, dict[str, str]] = {
    "numpy": {
        "license_concluded": "compuesta: BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 AND Apache-2.0 AND (GPL-3.0-or-later WITH GCC-exception-3.1, solo aviso)",
        "additional_obligations": "LICENSE es un archivo AGREGADO (45 KB) con avisos de terceros vendorizados (libdivide Zlib, highway Apache-2.0/CC0, x86-simd-sort BSD-3, pocketfft BSD-3, lapack-lite BSD-3, distribuciones MIT, svml). Se debe reproducir completo, no sólo la licencia del paquete",
        "compliance_status": "OBLIGACION_REGISTRADA_LICENCIA_AGREGADA",
        "remediation_phase": "PR-LIC-04",
        "unresolved_questions": "El bloque GPL-3.0 WITH GCC-exception del archivo agregado corresponde al aviso de la excepción de runtime de GCC, no a código GPL distribuido: conviene confirmarlo archivo por archivo antes de afirmarlo en el NOTICE",
    },
    "pillow": {
        "license_concluded": "MIT-CMU (HPND) + avisos agregados (Apache-2.0, Zlib, BSD-2, BSD-3, MIT)",
        "additional_obligations": "El LICENSE (78 KB) agrega libjpeg-turbo, zlib, libimagequant, etc. Se debe reproducir completo y conservar todos los copyright",
        "compliance_status": "OBLIGACION_REGISTRADA_LICENCIA_AGREGADA",
        "remediation_phase": "PR-LIC-04",
        "unresolved_questions": "El metadato declara MIT-CMU; HPND figura en el texto: se concluye la familia HPND/MIT-CMU, no 'MIT' genérica",
    },
    "grpcio": {
        "license_concluded": "Apache-2.0 AND BSD-3-Clause AND MPL-2.0 (archivo de licencia agregado)",
        "source_obligation": "SI",
        "additional_obligations": "El LICENSE incluye texto MPL-2.0 completo para una parte del árbol: si esos archivos están en el wheel distribuido nace obligación de fuente (LIC01-H). Requiere identificar qué parte es MPL",
        "compliance_status": "LEGAL_REVIEW_REQUIRED",
        "remediation_phase": "PR-LIC-02",
        "unresolved_questions": "¿Qué archivos del wheel de grpcio están bajo MPL-2.0? Sin el sdist no puede mapearse",
    },
    "aiohttp": {
        "license_concluded": "Apache-2.0 AND MIT (llhttp vendorizado)",
        "additional_obligations": "El wheel trae dist-info/licenses/vendor/llhttp/LICENSE (MIT) para el parser HTTP vendorizado: exige atribución separada de Fedor Indutny",
        "compliance_status": "OBLIGACION_REGISTRADA",
    },
    "cryptography": {
        "license_concluded": "Apache-2.0 OR BSD-3-Clause (elección del destinatario)",
        "additional_obligations": "Ambos textos viajan en el wheel (LICENSE.APACHE y LICENSE.BSD); el archivo LICENSE es sólo un puntero de 197 bytes. La elección debe declararse explícitamente en el NOTICE",
        "unresolved_questions": "La elección de licencia (Apache-2.0 vs BSD-3) debe quedar documentada en el paquete de avisos",
    },
    "orjson": {
        "license_concluded": "Apache-2.0 OR MIT",
        "additional_obligations": "License-Expression declara 'MPL-2.0 AND (Apache-2.0 OR MIT)' pero el wheel NO incluye texto MPL-2.0: falta evidencia de la parte MPL",
        "evidence_status": "EVIDENCE_CONFLICT",
        "compliance_status": "LEGAL_REVIEW_REQUIRED",
        "remediation_phase": "PR-LIC-02",
        "unresolved_questions": "¿Qué parte de orjson queda bajo MPL-2.0 y con qué obligación de fuente?",
    },
    "pycryptodomex": {
        "license_concluded": "Unlicense OR BSD-2-Clause (LICENSE.rst declara ambos regímenes por zona del árbol)",
        "additional_obligations": "LICENSE.rst (2987 B) contiene el texto Unlicense y el BSD-2 para partes concretas; hay que preservar la distinción dominio público / BSD-2",
    },
    "certifi": {
        "license_concluded": "MPL-2.0",
        "additional_obligations": "El único archivo (989 B) NOMBRA MPL-2.0 y describe un ca-bundle modificado, pero NO incluye el texto: hay que incorporar el texto íntegro de MPL-2.0 desde la fuente canónica",
        "evidence_status": "EVIDENCE_EXACT_ARTIFACT_TEXTO_INCOMPLETO",
        "compliance_status": "OBLIGACION_REGISTRADA_TEXTO_FALTANTE",
        "source_obligation": "SI",
        "remediation_phase": "PR-LIC-02",
        "unresolved_questions": "Si se considera que el bundle de CA es 'Covered Software' modificado, MPL-2.0 §3.1 exige disponibilidad del código fuente de la forma modificada",
    },
    "prometheus-client": {
        "license_concluded": "Apache-2.0",
        "additional_obligations": "El NOTICE declara que empaqueta 'decorator 4.0.10' bajo BSD-2 pero el wheel NO trae ese texto: falta un aviso obligatorio",
        "compliance_status": "OBLIGACION_REGISTRADA_TEXTO_FALTANTE",
        "remediation_phase": "PR-LIC-04",
        "unresolved_questions": "¿Sigue existiendo decorator.py vendorizado en 0.25.0? El NOTICE no se actualizó necesariamente",
    },
    "python-json-logger": {
        "license_concluded": "BSD-2-Clause",
        "additional_obligations": "El NOTICE menciona código de mkdocstrings-python (ISC) en el paquete: hay que decidir si aplica al binario distribuido",
        "compliance_status": "OBLIGACION_REGISTRADA",
        "unresolved_questions": "El NOTICE parece un remanente del proyecto de documentación; verificar si ese código viaja en el wheel",
    },
    "docutils": {
        "license_concluded": "",
        "evidence_status": "EVIDENCE_CONFLICT",
        "compliance_status": "LEGAL_REVIEW_REQUIRED",
        "remediation_phase": "PR-LIC-02",
        "unresolved_questions": "COPYING.rst describe un régimen mixto (dominio público / BSD-2 / GPL-2 para partes concretas): no puede concluirse una licencia única desde el texto",
    },
    "webencodings": {
        "license_concluded": "",
        "evidence_status": "EVIDENCE_MISSING",
        "compliance_status": "LEGAL_REVIEW_REQUIRED",
        "remediation_phase": "PR-LIC-02",
        "unresolved_questions": "Ni el wheel ni el sdist (ambos con hash verificado) contienen archivo de licencia: el texto BSD del upstream debe recuperarse del repositorio y verificarse",
    },
    "cffi": {
        "license_concluded": "MIT-0",
        "additional_obligations": "El texto del artefacto se rotula 'MIT No Attribution' y omite la cláusula de atribución: se concluye MIT-0 aunque el metadato declare MIT. No se puede exigir atribución que el texto no pide",
        "evidence_status": "EVIDENCE_CONFLICT",
        "compliance_status": "OBLIGACION_REGISTRADA_DISCREPANCIA_METADATO",
        "unresolved_questions": "Discrepancia metadato (MIT) vs texto (MIT-0): se documenta, no se resuelve por conveniencia",
    },
    "py7zr": {
        "license_concluded": "LGPL-2.1-or-later",
        "additional_obligations": "El artefacto trae el texto base de LGPL 2.1 (no incluye el aviso por archivo 'or later'): la condición or-later proviene de la declaración del proyecto. Obligaciones de fuente y relinking: PR-LIC-02",
        "unresolved_questions": "Falta el aviso por archivo que fija or-later; hay que fijarlo con el sdist, no por inferencia",
    },
    "pybcj": {
        "license_concluded": "LGPL-2.1-or-later",
        "additional_obligations": "Igual que py7zr: texto base LGPL 2.1 en el artefacto, or-later por declaración del proyecto",
        "unresolved_questions": "Igual que py7zr",
    },
    "pyppmd": {
        "license_concluded": "LGPL-2.1-or-later",
        "additional_obligations": "Igual que py7zr",
        "unresolved_questions": "Igual que py7zr",
    },
    "inflate64": {
        "license_concluded": "LGPL-2.1-or-later",
        "additional_obligations": "Igual que py7zr",
        "unresolved_questions": "Igual que py7zr",
    },
    "multivolumefile": {
        "license_concluded": "LGPL-2.1-or-later",
        "additional_obligations": "Igual que py7zr",
        "unresolved_questions": "Igual que py7zr",
    },
    "sv-ttk": {
        "redistribution_scope": "NO_ALCANZABLE_POR_IMPORT — declarada en pyproject, no importada por el código",
        "compliance_status": "REQUIERE_VERIFICACION_CON_ANALYSIS_TOC",
        "unresolved_questions": "sv-ttk está en pyproject/lock pero ningún módulo la importa: probablemente NO se congela. Confirmar con Analysis TOC antes de excluirla del NOTICE",
    },
}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--evidence", required=True)
    ap.add_argument("--salida", required=True)
    args = ap.parse_args(argv)

    ev = Path(args.evidence)
    red = json.loads((ev / "redistribucion.json").read_text(encoding="utf-8"))
    analisis = {d["paquete"]: d for d in json.loads((ev / "pypi" / "analisis_licencias.json").read_text(encoding="utf-8"))}
    metas = _pkgs(ev / "pypi")

    filas: list[dict[str, str]] = []
    for f in red["filas"]:
        pkg_dir = next((k for k in metas if k.lower().startswith(f["distribution"].lower() + "-")), None)
        if pkg_dir is None:
            continue
        version = pkg_dir[len(f["distribution"]) + 1 :]
        fila = fila_python(f["distribution"], version, f, analisis.get(pkg_dir, {}), metas[pkg_dir])
        for clave, valor in OVERRIDES.get(f["distribution"].lower(), {}).items():
            fila[clave] = valor
        fila["build_profiles"] = perfiles(f, {})
        filas.append(fila)

    # Componentes no-PyPI: tabla curada (cada valor debe poder rastrearse a evidence/ o al repo).
    extra = json.loads((ev / "componentes_no_pypi.json").read_text(encoding="utf-8"))
    for fila in extra:
        filas.append({k: str(fila.get(k, "")) for k in CAMPOS})

    filas.sort(key=lambda x: (x["component_id"].split("::")[0], x["component_id"].lower()))
    destino = Path(args.salida)
    with destino.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=CAMPOS)
        w.writeheader()
        for fila in filas:
            w.writerow({k: fila.get(k, "") for k in CAMPOS})

    resumen = {
        "filas": len(filas),
        "por_grupo": {},
        "por_evidence_status": {},
        "por_compliance_status": {},
        "con_source_obligation": sum(1 for f in filas if f.get("source_obligation") == "SI"),
    }
    for f in filas:
        g = f["component_id"].split("::")[0]
        resumen["por_grupo"][g] = resumen["por_grupo"].get(g, 0) + 1
        resumen["por_evidence_status"][f["evidence_status"]] = resumen["por_evidence_status"].get(f["evidence_status"], 0) + 1
        resumen["por_compliance_status"][f["compliance_status"]] = resumen["por_compliance_status"].get(f["compliance_status"], 0) + 1
    Path(args.salida).with_name("matriz_resumen.json").write_text(json.dumps(resumen, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(resumen, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(__import__("sys").argv[1:]))

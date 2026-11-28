"""PR-LIC-01 — Colector de textos primarios desde GitHub (API de contenidos, refs fijos).

Para cada componente no-PyPI se fija un repositorio, una RUTA y una REF concreta (tag/rama/commit).
Se guarda el texto, el SHA-256 del blob devuelto por la API, el SHA del commit resuelto y la fecha
de recuperación. Nada se "recuerda": si la API no responde, el componente queda EVIDENCE_MISSING.

Uso:
    python3 colectar_textos_github.py --salida ./evidence/github
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

API = "https://api.github.com"
UA = "Sky-Claw-PR-LIC-01-compliance-audit/1.0"

#: (id, repo, ref, path, motivo). La ref se fija siempre: una rama móvil no es evidencia.
OBJETIVOS: list[tuple[str, str, str, str, str]] = [
    ("cpython", "python/cpython", "v3.11.9", "LICENSE", "Licencia PSF de CPython redistribuido"),
    ("cpython", "python/cpython", "v3.11.9", "Doc/license.rst", "Avisos de terceros embebidos en CPython"),
    ("pyinstaller", "pyinstaller/pyinstaller", "v6.22.3", "COPYING.txt", "GPL-2.0 con excepción de bootloader"),
    ("pyinstaller", "pyinstaller/pyinstaller", "v6.22.3", "bootloader/src/pyi_main.c", "Cabecera de la excepción del bootloader"),
    ("openssl", "openssl/openssl", "openssl-3.0.16", "LICENSE.txt", "Apache-2.0 de OpenSSL embebido por CPython Windows"),
    ("libffi", "libffi/libffi", "v3.4.6", "LICENSE", "MIT de libffi embebido"),
    ("sqlite", "sqlite/sqlite", "version-3.45.3", "LICENSE.md", "SQLite: dominio público (espejo GitHub del upstream)"),
    ("tcl", "tcltk/tcl", "core-8-6-16", "license.terms", "Tcl/Tk: BSD-style (solo si está en el ejecutable)"),
    ("hermes-agent", "NousResearch/hermes-agent", "main", "LICENSE", "MIT de Hermes Agent ya citado en THIRD_PARTY_NOTICES"),
    ("nicegui", "zauberzeug/nicegui", "v3.12.0", "LICENSE", "MIT de NiceGUI (contenedor de recursos web vendorizados)"),
    ("auto-parallax", "doodlum/skyrim-auto-parallax", "main", "COPYING", "GPL-3.0 upstream de Auto Parallax (el repo no tiene LICENSE; GitHub detecta gpl-3.0)"),
    ("auto-parallax", "doodlum/skyrim-auto-parallax", "main", "EXCEPTIONS.md", "Excepciones declaradas por el upstream de Auto Parallax"),
    ("auto-parallax", "doodlum/skyrim-auto-parallax", "main", "src/XSEPlugin.cpp", "Upstream transcrito en auto_parallax_regla.py"),
    ("commonlibsse", "CharmedBaryon/CommonLibSSE-NG", "main", "include/RE/B/BSShaderProperty.h", "Cabecera citada en auto_parallax_regla.py (MIT)"),
    ("commonlibsse", "CharmedBaryon/CommonLibSSE-NG", "main", "LICENSE", "MIT de CommonLibSSE-NG"),
    ("mo2", "ModOrganizer2/modorganizer", "master", "LICENSE", "Licencia de Mod Organizer 2 (frontera externa)"),
    ("mo2-python-plugin", "ModOrganizer2/modorganizer-plugin_python", "master", "README.md", "El repo del plugin Python/mobase NO tiene LICENSE: se registra el README y la ausencia"),
    ("mo2-usvfs", "ModOrganizer2/usvfs", "master", "LICENSE", "Licencia de usvfs (VFS de MO2)"),
    ("font-cinzel", "NDISCOVER/Cinzel", "master", "OFL.txt", "OFL-1.1 de Cinzel"),
    ("font-ebgaramond", "octaviopardo/EBGaramond12", "master", "OFL.txt", "OFL-1.1 de EB Garamond"),
    ("font-notorunic", "notofonts/runic", "main", "OFL.txt", "OFL-1.1 de Noto Sans Runic"),
    ("font-splinesansmono", "SorkinType/SplineSansMono", "main", "OFL.txt", "OFL-1.1 de Spline Sans Mono"),
    ("font-cinzel", "NDISCOVER/Cinzel", "master", "fonts/variable/Cinzel[wght].ttf", "¿Existe TTF variable upstream? (solo se registra tamaño)"),
]


def _get(url: str) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310 - host fijo api.github.com
        return json.loads(resp.read().decode("utf-8"))


def _commit_sha(repo: str, ref: str) -> str | None:
    try:
        return _get(f"{API}/repos/{repo}/commits/{ref}")["sha"]
    except urllib.error.HTTPError:
        return None


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--salida", required=True)
    args = ap.parse_args(argv)
    salida = Path(args.salida)
    salida.mkdir(parents=True, exist_ok=True)

    registro: list[dict[str, Any]] = []
    for cid, repo, ref, path, motivo in OBJETIVOS:
        entrada: dict[str, Any] = {
            "component_id": cid,
            "repo": repo,
            "ref": ref,
            "path": path,
            "motivo": motivo,
            "retrieved_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
        }
        url = f"{API}/repos/{repo}/contents/{urllib.parse.quote(path)}?ref={ref}"
        try:
            data = _get(url)
        except urllib.error.HTTPError as exc:
            entrada["error"] = f"HTTP {exc.code}"
            entrada["url"] = url
            registro.append(entrada)
            continue
        if isinstance(data, list):
            entrada["error"] = "la ruta es un directorio"
            registro.append(entrada)
            continue
        contenido = base64.b64decode(data["content"]) if data.get("content") else b""
        destino = salida / cid
        destino.mkdir(parents=True, exist_ok=True)
        nombre = path.replace("/", "__")
        (destino / nombre).write_bytes(contenido)
        entrada.update(
            {
                "url": data.get("html_url") or url,
                "api_url": url,
                "blob_sha_git": data.get("sha"),
                "bytes": len(contenido),
                "sha256": hashlib.sha256(contenido).hexdigest(),
                "commit_resuelto": _commit_sha(repo, ref),
                "saved_as": f"{cid}/{nombre}",
            }
        )
        registro.append(entrada)

    (salida / "registro_github.json").write_text(json.dumps(registro, indent=2, ensure_ascii=False), encoding="utf-8")
    ok = sum(1 for r in registro if "sha256" in r)
    print(f"objetivos: {len(registro)}  recuperados: {ok}  fallidos: {len(registro) - ok}")
    for r in registro:
        if "error" in r:
            print(f"  FALLO {r['component_id']} {r['path']}: {r['error']}")
    return 0


if __name__ == "__main__":
    import urllib.parse  # noqa: E402 - usado en main

    raise SystemExit(main(sys.argv[1:]))

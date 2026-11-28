"""PR-LIC-01 / LIC01-A — Reconciliación «¿qué se redistribuye realmente?».

Problema que resuelve: un lockfile NO prueba redistribución. PyInstaller congela solo lo que el
análisis alcanza desde ``sky_claw/__main__.py`` (imports estáticos) más ``hiddenimports`` del spec.
Por eso:

1. Se extraen los imports de primer nivel del código propio (AST, no regex).
2. Se suman los ``hiddenimports`` del spec (que fuerzan inclusión aunque el import sea perezoso).
3. Se cierra transitivamente con ``requires_dist`` del metadato PyPI de cada paquete del lock.
4. Se contrasta contra el lock completo: lo que no es alcanzable queda como
   ``NO_ALCANZABLE_POR_IMPORT`` (candidato a build-only o a falso positivo), NO como "no
   redistribuido" definitivo: eso exige el Analysis TOC, que en esta fase es EVIDENCE_MISSING.

Salida: ``redistribucion.json`` con la clasificación por distribución.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path
from typing import Any

#: Nombre de distribución → módulos de primer nivel, solo para los casos NO triviales.
#: El resto se resuelve con ``-`` → ``_`` y se valida contra los imports encontrados.
EXCEPCIONES_MODULO: dict[str, list[str]] = {
    "pywin32-ctypes": ["win32ctypes"],
    "pycryptodomex": ["Cryptodome"],
    "python-dotenv": ["dotenv"],
    "python-multipart": ["multipart"],
    "python-engineio": ["engineio"],
    "python-socketio": ["socketio"],
    "python-json-logger": ["pythonjsonlogger"],
    "opentelemetry-api": ["opentelemetry"],
    "opentelemetry-sdk": ["opentelemetry"],
    "opentelemetry-proto": ["opentelemetry"],
    "opentelemetry-semantic-conventions": ["opentelemetry"],
    "opentelemetry-exporter-otlp-common": ["opentelemetry"],
    "opentelemetry-exporter-otlp-proto-common": ["opentelemetry"],
    "opentelemetry-exporter-otlp-proto-grpc": ["opentelemetry"],
    "googleapis-common-protos": ["google"],
    "protobuf": ["google"],
    "grpcio": ["grpc"],
    "typing-extensions": ["typing_extensions"],
    "typing-inspection": ["typing_inspection"],
    "importlib-metadata": ["importlib_metadata"],
    "jaraco-classes": ["jaraco"],
    "jaraco-context": ["jaraco"],
    "jaraco-functools": ["jaraco"],
    "backports-tarfile": ["backports"],
    "backports-zstd": ["backports"],
    "sv-ttk": ["sv_ttk"],
    "lxml-html-clean": ["lxml_html_clean"],
    "pydantic-core": ["pydantic_core"],
    "more-itertools": ["more_itertools"],
    "aiosqlite": ["aiosqlite"],
    "aiohappyeyeballs": ["aiohappyeyeballs"],
    "annotated-types": ["annotated_types"],
    "annotated-doc": ["annotated_doc"],
    "inflate64": ["inflate64"],
    "multivolumefile": ["multivolumefile"],
    "pybcj": ["pybcj"],
    "pyppmd": ["pyppmd"],
    "simple-websocket": ["simple_websocket"],
    "jaraco.text": ["jaraco"],
    "mccabe": ["mccabe"],
    "pefile": ["pefile"],
    "texttable": ["texttable"],
    "watchfiles": ["watchfiles"],
    "websockets": ["websockets"],
    "wsproto": ["wsproto"],
    "zope-interface": ["zope"],
    "pyyaml": ["yaml"],
    "pillow": ["PIL"],
}

#: Nombres de import que NO corresponden a una distribución del lock (stdlib o first-party).
STDLIB_E_INTERNOS = {"sky_claw", "__future__", "typing", "json", "os", "sys", "re", "ast", "pathlib"}


def modulos_de(nombre: str) -> list[str]:
    if nombre in EXCEPCIONES_MODULO:
        return EXCEPCIONES_MODULO[nombre]
    return [nombre.replace("-", "_").lower()]


def imports_propios(raiz: Path) -> set[str]:
    vistos: set[str] = set()
    for archivo in raiz.rglob("*.py"):
        try:
            arbol = ast.parse(archivo.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        for nodo in ast.walk(arbol):
            if isinstance(nodo, ast.Import):
                for a in nodo.names:
                    vistos.add(a.name.split(".")[0])
            elif isinstance(nodo, ast.ImportFrom):
                if nodo.level == 0 and nodo.module:
                    vistos.add(nodo.module.split(".")[0])
    return vistos


def hiddenimports_del_spec(spec: Path) -> set[str]:
    texto = spec.read_text(encoding="utf-8")
    bloque = re.search(r"hiddenimports\s*=\s*\[(.*?)\n\]", texto, re.S)
    if not bloque:
        return set()
    return set(re.findall(r'"([A-Za-z0-9_.]+)"', bloque.group(1)))


def leer_lock(ruta: Path) -> dict[str, str]:
    return dict(re.findall(r"^([A-Za-z0-9_.\-]+)==([^\s\\]+)", ruta.read_text(encoding="utf-8"), re.M))


def norm_pep503(n: str) -> str:
    return re.sub(r"[-_.]+", "-", n).lower()


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--pypi-dir", required=True)
    ap.add_argument("--salida", required=True)
    args = ap.parse_args(argv)

    repo = Path(args.repo)
    pypi = Path(args.pypi_dir)
    imports = imports_propios(repo / "sky_claw")
    hidden = hiddenimports_del_spec(repo / "sky_claw.spec")

    lock_base = leer_lock(repo / "requirements.lock")
    lock_nat = leer_lock(repo / "requirements-native-parallax.lock")
    universo = sorted(set(lock_base) | set(lock_nat))

    # Metadatos PyPI ya recuperados (requires_dist) para el cierre transitivo.
    requires: dict[str, list[str]] = {}
    for d in pypi.iterdir():
        meta = d / "metadata.json"
        if not d.is_dir() or not meta.exists():
            continue
        info = json.loads(meta.read_text(encoding="utf-8"))["pypi_json"]["info"]
        reqs = []
        for r in info.get("requires_dist") or []:
            # descarta dependencias marcadas como extra opcional salvo que el extra lo usemos
            reqs.append(r)
        requires[norm_pep503(info["name"])] = reqs

    # Semilla: imports propios ∪ hiddenimports (solo los de primer nivel)
    semilla_mods = {m for m in imports if m not in STDLIB_E_INTERNOS}
    semilla_mods |= {h.split(".")[0] for h in hidden}

    # módulo de primer nivel → TODAS las distribuciones que lo proveen (opentelemetry-* comparten
    # ``opentelemetry``; protobuf y googleapis-common-protos comparten ``google``). Un mapeo 1:1
    # perdería paquetes reales: ese error se detectó al contrastar con ``tracing.py``.
    mod_a_dists: dict[str, set[str]] = {}
    for n in universo:
        for m in modulos_de(n):
            mod_a_dists.setdefault(m.lower(), set()).add(n)

    modulos_alcanzados = {m.lower() for m in semilla_mods}
    alcanzados: set[str] = set()
    cambio = True
    while cambio:
        cambio = False
        for m in list(modulos_alcanzados):
            for dist in mod_a_dists.get(m, ()):
                if dist not in alcanzados:
                    alcanzados.add(dist)
                    cambio = True
                    for mm in modulos_de(dist):
                        if mm.lower() not in modulos_alcanzados:
                            modulos_alcanzados.add(mm.lower())
                            cambio = True
        for dist in list(alcanzados):
            for req in requires.get(norm_pep503(dist), []):
                nombre = re.split(r"[<>=!~;\[ ]", req.strip())[0]
                cand = norm_pep503(nombre or "")
                for real in universo:
                    if norm_pep503(real) == cand and real not in alcanzados:
                        alcanzados.add(real)
                        cambio = True
                        for mm in modulos_de(real):
                            modulos_alcanzados.add(mm.lower())

    filas = []
    for n in universo:
        mods = modulos_de(n)
        import_directo = sorted(m for m in mods if m.lower() in {s.lower() for s in semilla_mods})
        filas.append(
            {
                "distribution": n,
                "canonical": norm_pep503(n),
                "modules": mods,
                "lock_base": n in lock_base,
                "lock_native_parallax": n in lock_nat,
                "import_directo_o_hidden": import_directo,
                "alcanzable_transitivo": n in alcanzados,
                "redistribucion_probable": n in alcanzados,
                "motivo": (
                    "import directo/hidden"
                    if import_directo
                    else ("dependency closure" if n in alcanzados else "no alcanzable desde __main__ ni hiddenimports")
                ),
            }
        )

    resumen = {
        "imports_de_primer_nivel_en_sky_claw": sorted(imports),
        "hiddenimports_del_spec": sorted(hidden),
        "distribuciones_en_lock": len(universo),
        "alcanzables": sum(1 for f in filas if f["alcanzable_transitivo"]),
        "no_alcanzables": sorted(f["distribution"] for f in filas if not f["alcanzable_transitivo"]),
        "nota": (
            "«no alcanzable» NO equivale a «no redistribuido»: puede entrar por un hook de PyInstaller "
            "o por import dinámico no declarado. La prueba definitiva es el Analysis TOC (EVIDENCE_MISSING en esta fase)."
        ),
        "filas": filas,
    }
    Path(args.salida).write_text(json.dumps(resumen, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"distribuciones: {len(universo)}  alcanzables: {resumen['alcanzables']}  no alcanzables: {len(resumen['no_alcanzables'])}")
    print("no alcanzables:", ", ".join(resumen["no_alcanzables"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

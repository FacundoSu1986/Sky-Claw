"""PR-LIC-01 / LIC01-A — Colector de evidencia primaria desde PyPI.

Qué hace, y qué NO hace:
- Lee los lockfiles del repo (``requirements.lock`` = perfil base, ``requirements-native-parallax.lock``
  = perfil native-parallax) y extrae nombre/versión/hashes declarados por uv.
- Consulta el JSON oficial de PyPI para la versión EXACTA fijada en el lock (no "latest").
- Descarga el artefacto de distribución cuya SHA-256 figure en el lock (preferencia: wheel
  ``win_amd64``/``cp311``, luego wheel ``any``, luego sdist), VERIFICA el hash contra el lock y
  extrae únicamente los archivos de licencia/aviso del interior.
- Registra URL, fecha de recuperación, SHA-256 del artefacto y SHA-256 de cada texto extraído.

Lo que NO hace:
- No concluye licencias. Solo recupera evidencia. La conclusión vive en la matriz (LIC01-A).
- No copia ``license``/``License-Expression`` de PyPI como licencia concluida.
- No modifica el repositorio ni instala nada.

Uso:
    python3 colectar_metadatos_pypi.py --repo-root /ruta/al/repo --salida ./evidence
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import sys
import tarfile
import time
import urllib.error
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PYPI_JSON = "https://pypi.org/pypi/{name}/{version}/json"
UA = "Sky-Claw-PR-LIC-01-compliance-audit/1.0 (+https://github.com/FacundoSu1986/Sky-Claw)"
MAX_BYTES = 120 * 1024 * 1024
LICENSE_NAME_RE = re.compile(
    r"^(licen[sc]e|copying|copyright|notice|authors|ofl|unlicense)([._-].*)?$", re.IGNORECASE
)
LICENSE_EXT_OK = {".txt", ".md", ".rst", "", ".1", ".mit", ".bsd", ".apache"}


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _get(url: str, *, binary: bool = False, timeout: int = 60) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - host fijo pypi.org
        data = resp.read()
    return data if binary else json.loads(data.decode("utf-8"))


def leer_locks(repo_root: Path) -> dict[str, dict[str, set[str]]]:
    """Devuelve {perfil: {nombre_canonico: {hashes...}}} a partir de los lockfiles de uv."""
    perfiles = {
        "base": repo_root / "requirements.lock",
        "native-parallax": repo_root / "requirements-native-parallax.lock",
    }
    salida: dict[str, dict[str, set[str]]] = {}
    for perfil, ruta in perfiles.items():
        texto = ruta.read_text(encoding="utf-8")
        paquetes: dict[str, set[str]] = {}
        actual: str | None = None
        for linea in texto.splitlines():
            m = re.match(r"^([A-Za-z0-9_.\-]+)==([^\s\\]+)", linea)
            if m:
                actual = m.group(1)
                paquetes[actual] = set()
                continue
            mh = re.search(r"--hash=sha256:([0-9a-f]{64})", linea)
            if mh and actual:
                paquetes[actual].add(mh.group(1))
        salida[perfil] = paquetes
    return salida


def _rank_artifact(fn: str, hashes: set[str]) -> tuple[int, str]:
    """Preferencia de artefacto: wheel cp311/win_amd64 (0) > wheel any (1) > wheel genérica (2) > sdist (3)."""
    low = fn.lower()
    if low.endswith(".whl"):
        if "win_amd64" in low and "cp311" in low:
            return (0, fn)
        if "win_amd64" in low:
            return (1, fn)
        if "py3-none-any" in low or "py2.py3-none-any" in low:
            return (2, fn)
        return (3, fn)
    return (4, fn)


def _extraer_textos(datos: bytes, nombre: str) -> dict[str, bytes]:
    """Extrae del artefacto los archivos de licencia/aviso, sin ejecutar nada."""
    salida: dict[str, bytes] = {}
    if nombre.lower().endswith(".whl"):
        with zipfile.ZipFile(io.BytesIO(datos)) as z:
            for info in z.infolist():
                base = Path(info.filename).name
                if info.is_dir():
                    continue
                # .dist-info/{LICENSE,COPYING,NOTICE,...}, .dist-info/licenses/... y licencias de nivel raíz
                partes = info.filename.split("/")
                en_dist_info = any(p.endswith((".dist-info", ".egg-info")) for p in partes[:-1])
                if en_dist_info or len(partes) == 1:
                    if LICENSE_NAME_RE.match(base) and Path(base).suffix.lower() in LICENSE_EXT_OK:
                        if info.file_size <= 512 * 1024:
                            salida[info.filename] = z.read(info)
    elif nombre.lower().endswith((".tar.gz", ".tgz", ".zip")):
        if nombre.lower().endswith(".zip"):
            with zipfile.ZipFile(io.BytesIO(datos)) as z:
                for info in z.infolist():
                    if info.is_dir():
                        continue
                    if "/" in info.filename:
                        prof = info.filename.split("/", 1)[1]
                    else:
                        prof = info.filename
                    if "/" in prof:
                        continue  # solo nivel raíz del sdist
                    if LICENSE_NAME_RE.match(Path(prof).name) and info.file_size <= 512 * 1024:
                        salida[prof] = z.read(info)
        else:
            with tarfile.open(fileobj=io.BytesIO(datos), mode="r:gz") as t:
                for m in t.getmembers():
                    if not m.isfile():
                        continue
                    partes = m.name.split("/")
                    prof = "/".join(partes[1:]) if len(partes) > 1 else m.name
                    if "/" in prof:
                        continue
                    if LICENSE_NAME_RE.match(Path(prof).name) and m.size <= 512 * 1024:
                        f = t.extractfile(m)
                        if f is not None:
                            salida[prof] = f.read()
    return salida


def procesar(
    nombre: str, version: str, hashes: set[str], perfiles: list[str], salida: Path, registro: list[dict[str, Any]]
) -> dict[str, Any]:
    registro_pkg: dict[str, Any] = {
        "name": nombre,
        "version": version,
        "profiles": perfiles,
        "lock_hashes": sorted(hashes),
        "retrieved_at": _now(),
    }
    try:
        meta = _get(PYPI_JSON.format(name=nombre, version=version))
        registro.append({"retrieved_at": registro_pkg["retrieved_at"], "url": PYPI_JSON.format(name=nombre, version=version)})
    except urllib.error.HTTPError as exc:
        registro_pkg["metadata_error"] = f"HTTP {exc.code}"
        return registro_pkg

    info = meta["info"]
    registro_pkg["pypi_info"] = {
        "license": info.get("license"),
        "license_expression": info.get("license_expression"),
        "classifiers_license": [c for c in info.get("classifiers", []) if c.startswith("License ::")],
        "classifiers_all": info.get("classifiers", []),
        "requires_python": info.get("requires_python"),
        "project_urls": info.get("project_urls"),
        "home_page": info.get("home_page"),
        "summary": (info.get("summary") or "")[:300],
        "author": info.get("author_email") or info.get("author") or info.get("maintainer_email"),
    }
    candidatos = []
    for f in meta.get("urls", []):
        dig = (f.get("digests") or {}).get("sha256", "")
        if dig and hashes and dig not in hashes:
            continue  # el lock fija otros artefactos: no sustituimos evidencia
        candidatos.append((_rank_artifact(f["filename"], hashes), f, dig))
    cand_no_hash = []
    if not candidatos:
        for f in meta.get("urls", []):
            dig = (f.get("digests") or {}).get("sha256", "")
            cand_no_hash.append((_rank_artifact(f["filename"], hashes), f, dig))
        registro_pkg["artifact_hash_matched_lock"] = False
    else:
        registro_pkg["artifact_hash_matched_lock"] = True
    if not candidatos and not cand_no_hash:
        registro_pkg["artifact_error"] = "sin artefactos declarados en PyPI"
        return registro_pkg

    pool = candidatos or cand_no_hash
    pool.sort(key=lambda t: t[0])
    intentos = []
    for _rank, f, dig in pool:
        if f.get("size", 0) and f["size"] > MAX_BYTES:
            continue
        intentos.append((f, dig))
        if len(intentos) >= 6:
            break
    if not intentos:
        registro_pkg["artifact_error"] = "todos los artefactos exceden el tope de tamaño"
        return registro_pkg

    destino = salida / f"{nombre}-{version}"
    destino.mkdir(parents=True, exist_ok=True)
    registro_pkg["artifacts_tried"] = []
    textos: dict[str, Any] = {}
    for f, dig in intentos:
        registro_pkg["artifacts_tried"].append(f["filename"])
        entrada = {
            "filename": f["filename"],
            "url": f["url"],
            "packagetype": f.get("packagetype"),
            "size": f.get("size"),
            "sha256_pypi": dig,
            "sha256_verified": None,
        }
        try:
            datos = _get(f["url"], binary=True)
        except urllib.error.HTTPError as exc:
            entrada["error"] = f"HTTP {exc.code} al descargar"
            registro_pkg.setdefault("artifact_attempts", []).append(entrada)
            continue
        sha = hashlib.sha256(datos).hexdigest()
        entrada["sha256_verified"] = sha
        entrada["sha256_matches_pypi"] = sha == dig
        entrada["sha256_in_lock"] = sha in hashes
        registro.append({"retrieved_at": _now(), "url": f["url"], "sha256": sha})
        registro_pkg.setdefault("artifact_attempts", []).append(entrada)
        if registro_pkg.get("artifact") is None:
            registro_pkg["artifact"] = entrada
        try:
            extraidos = _extraer_textos(datos, f["filename"])
        except Exception as exc:  # noqa: BLE001 - evidencia, no ejecución
            registro_pkg.setdefault("extract_errors", []).append(f"{f['filename']}: {type(exc).__name__}: {exc}")
            extraidos = {}
        for ruta, contenido in extraidos.items():
            nombre_archivo = re.sub(r"[^A-Za-z0-9._-]+", "_", ruta)
            (destino / nombre_archivo).write_bytes(contenido)
            textos[f"{f['filename']}::{ruta}"] = {
                "saved_as": nombre_archivo,
                "sha256": hashlib.sha256(contenido).hexdigest(),
                "bytes": len(contenido),
                "from_artifact": f["filename"],
                "artifact_sha256": sha,
            }
        if textos:
            break
    registro_pkg["license_texts_extracted"] = textos
    registro_pkg["license_texts_expected_but_missing"] = len(textos) == 0
    (destino / "metadata.json").write_text(
        json.dumps({"pypi_json": meta, "registro": registro_pkg}, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return registro_pkg


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo-root", required=True)
    ap.add_argument("--salida", required=True)
    ap.add_argument("--workers", type=int, default=12)
    args = ap.parse_args(argv)

    repo = Path(args.repo_root)
    salida = Path(args.salida)
    pypi_dir = salida / "pypi"
    pypi_dir.mkdir(parents=True, exist_ok=True)

    locks = leer_locks(repo)
    base = locks["base"]
    native = locks["native-parallax"]
    universo: dict[str, dict[str, Any]] = {}
    for perfil, paquetes in locks.items():
        for n, hs in paquetes.items():
            e = universo.setdefault(n, {"hashes": set(), "perfiles": []})
            e["hashes"] |= hs
            e["perfiles"].append(perfil)

    # versión por nombre (los dos locks deben coincidir; si no, se registra el conflicto)
    versiones: dict[str, dict[str, list[str]]] = {}
    for perfil, ruta in (("base", repo / "requirements.lock"), ("native-parallax", repo / "requirements-native-parallax.lock")):
        texto = ruta.read_text(encoding="utf-8")
        for m in re.finditer(r"^([A-Za-z0-9_.\-]+)==([^\s\\]+)", texto, re.M):
            versiones.setdefault(m.group(1), {}).setdefault(m.group(2), []).append(perfil)

    registro: list[dict[str, Any]] = []
    resultados: list[dict[str, Any]] = []
    tareas = []
    for nombre in sorted(universo):
        vs = versiones.get(nombre, {})
        if len(vs) != 1:
            resultados.append({"name": nombre, "version": "CONFLICT", "versions_seen": vs, "profiles": universo[nombre]["perfiles"]})
            continue
        version = next(iter(vs))
        tareas.append((nombre, version, universo[nombre]["hashes"], universo[nombre]["perfiles"]))

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futuros = [ex.submit(procesar, n, v, h, p, pypi_dir, registro) for n, v, h, p in tareas]
        for fut in futuros:
            try:
                resultados.append(fut.result())
            except Exception as exc:  # noqa: BLE001
                resultados.append({"name": "UNKNOWN", "error": f"{type(exc).__name__}: {exc}"})

    (pypi_dir / "resumen.json").write_text(json.dumps(resultados, indent=2, ensure_ascii=False), encoding="utf-8")
    reg = salida / "registry"
    reg.mkdir(parents=True, exist_ok=True)
    with (reg / "urls_y_fechas.jsonl").open("w", encoding="utf-8") as fh:
        for r in registro:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")

    print(f"paquetes procesados: {len(resultados)}")
    con_texto = sum(1 for r in resultados if r.get("license_texts_extracted"))
    sin_texto = sum(1 for r in resultados if r.get("license_texts_expected_but_missing"))
    print(f"con texto de licencia extraído: {con_texto}; sin texto: {sin_texto}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

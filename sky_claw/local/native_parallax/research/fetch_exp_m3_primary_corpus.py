"""EXP-M3 §4/§8 — adquisición del corpus primario (research-only; NUNCA en CI).

Fuentes primarias oficiales, sin mirrors:

- **Poly Haven** (CC0, https://polyhaven.com/license): la API oficial
  ``api.polyhaven.com/files/<id>`` publica **md5 por archivo** → si nuestros bytes
  coinciden, la entrada queda ``OFFICIAL_HASH_VERIFIED``.
- **ambientCG** (CC0 1.0, https://docs.ambientcg.com/license/): la API v2 oficial
  entrega el ``downloadLink`` estable del zip ``1K-PNG`` (NormalGL PNG lossless +
  Displacement PNG **16-bit**) y NO publica checksums → la entrada queda
  ``PRIMARY_SOURCE_DOWNLOADED`` (sha256 propio calculado post-descarga).

Reglas (§8/§12/§14):
- los binarios viven FUERA del repo (--data-root); el repo sólo recibe el manifest
  con metadata/hashes/split.
- ``originals/`` no se modifica después de descargar: se preserva el zip original
  completo de ambientCG y los PNG originales de Poly Haven, sin transformar.
- el par normal+displacement de cada asset sale del MISMO asset id y del MISMO set
  de resolución; cualquier mismatch es error (no se "arregla" con otro archivo).

Uso (operador, desde el root del repo):

    python -m sky_claw.local.native_parallax.research.fetch_exp_m3_primary_corpus \\
        --data-root C:\\SkyClawResearch\\NativeParallax\\EXP-M3 \\
        --out docs/design/research/native-parallax/data/exp-m3-clean-authored-manifest.json

Flags: ``--only polyhaven|ambientcg`` limita la descarga; ``--build-only`` regenera
source.json/manifest desde archivos ya descargados (sin red).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.request
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit

from PIL import Image

from sky_claw.local.native_parallax.research.run_exp_m2 import split_of
from sky_claw.local.native_parallax.research.run_exp_m3 import VALID_PROVENANCE

USER_AGENT = "skyclaw-exp-m3-research/1.0"
PH_FILES_API = "https://api.polyhaven.com/files/{asset_id}"
PH_ASSET_PAGE = "https://polyhaven.com/a/{asset_id}"
PH_LICENSE_URL = "https://polyhaven.com/license"
ACG_API = "https://ambientcg.com/api/v2/full_json?id={asset_id}&include=downloadData"
ACG_ASSET_PAGE = "https://ambientcg.com/a/{asset_id}"
ACG_LICENSE_URL = "https://docs.ambientcg.com/license/"
LICENSE_ID = "CC0-1.0"

# Selección pre-registrada por provenance/licencia/familia/par válido — NO por
# resultado de reconstrucción (§17). Familias alineadas a la taxonomía EXP-M2 para
# que el split pre-registrado (§18; CALIBRATION_FAMILIES de fetch_exp_m2_corpus) se
# aplique sin cambios: brick / wood_planks / rock = CALIBRATION; resto = HELD_OUT.
POLYHAVEN_ASSETS: dict[str, str] = {
    "brick_4": "brick",
    "brick_wall_005": "brick",
    "red_brick_03": "brick",
    "castle_brick_07": "brick",
    "brown_planks_03": "wood_planks",
    "brown_planks_05": "wood_planks",
    "dark_wooden_planks": "wood_planks",
    "weathered_planks": "wood_planks",
    "rock_face": "rock",
    "gray_rocks": "rock",
    "rocky_terrain": "rock",
    "cliff_side": "rock",
    "cobblestone_01": "stone",
    "cobblestone_03": "stone",
    "cobblestone_05": "stone",
    "brown_floor_tiles": "tiles",
    "blue_floor_tiles_01": "tiles",
    "brushed_concrete_03": "concrete",
    "chipped_concrete": "concrete",
    "square_brick_paving": "tiles_paving",
    "precast_stone_paving": "tiles_paving",
    "dry_ground_01": "ground",
    "cracked_red_ground": "ground",
}

AMBIENTCG_ASSETS: dict[str, str] = {
    "Bricks051": "brick",
    "Planks021": "wood_planks",
    "Rock023": "rock",
    "Tiles049": "tiles",
    "Tiles075": "tiles",
    "Concrete028": "concrete",
    "PavingStones131": "tiles_paving",
    "Ground054": "ground",
}

# El schema §10 exige estos campos por asset (además de los que ya valida el gate
# de procedencia de run_exp_m3.load_cohort_a).
REQUIRED_ENTRY_FIELDS = (
    "asset_id",
    "provider",
    "family",
    "official_asset_page",
    "official_download_url_or_identifier",
    "official_source",
    "license",
    "license_reference",
    "normal_filename_original",
    "height_filename_original",
    "normal_path",
    "height_path",
    "normal_sha256",
    "height_sha256",
    "normal_size_bytes",
    "height_size_bytes",
    "normal_resolution",
    "height_resolution",
    "normal_format",
    "height_format",
    "normal_convention",
    "height_semantics",
    "download_timestamp",
    "provenance_status",
)


class CorpusAcquisitionError(RuntimeError):
    """F2 — fallo de red/HTTP en adquisición: contexto preservado, sin URLs firmadas."""


class CorpusValidationError(RuntimeError):
    """Asset inválido para Cohort A (provenance/par/hash/split) — §14/§27."""


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def md5_file(path: Path) -> str:
    # md5 es el checksum publicado por Poly Haven: verificación de provenance,
    # NO primitiva criptográfica — usedforsecurity=False lo declara explícito.
    h = hashlib.md5(usedforsecurity=False)  # noqa: S324 - checksum de provenance, no criptografía
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def host_of(url: str) -> str:
    """F2: hostname sin query/path para mensajes de error sin credenciales."""
    return urlsplit(url).netloc


def _require_https_url(url: str) -> str:
    """F-harden: sólo URLs HTTPS válidas llegan a urlopen (sin userinfo embebido).

    Rechaza esquemas no-https, URLs inparseables, hostname ausente y
    user:password@host antes de construir la Request. Los mensajes de error
    usan hostname: nunca la query/path firmada completa.
    """
    try:
        parts = urlsplit(url)
    except ValueError as exc:
        raise CorpusValidationError("URL inparseable en adquisición de corpus") from exc
    if parts.scheme != "https":
        raise CorpusValidationError(
            f"esquema no permitido ({parts.scheme or 'vacío'}): sólo https en adquisición de corpus"
        )
    if parts.hostname is None:
        raise CorpusValidationError("URL sin hostname en adquisición de corpus")
    if parts.username is not None or parts.password is not None:
        raise CorpusValidationError(f"userinfo embebido no permitido en adquisición de {parts.hostname}")
    return url


def _http_get(url: str, *, timeout: int = 120) -> bytes:
    safe_url = _require_https_url(url)
    req = urllib.request.Request(safe_url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310 - URL prevalidated as HTTPS by _require_https_url
            payload: bytes = resp.read()
            return payload
    except HTTPError as exc:
        # F2: el status HTTP se preserva; la URL puede contener query firmada → no se loguea.
        raise CorpusAcquisitionError(f"HTTP {exc.code} en descarga de {host_of(url)}") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise CorpusAcquisitionError(f"red falló ({type(exc).__name__}) en {host_of(url)}") from exc


def _http_download(url: str, dest: Path, *, expected_size: int | None = None) -> None:
    """Descarga streaming con verificación de tamaño si el upstream lo publica."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    safe_url = _require_https_url(url)
    req = urllib.request.Request(safe_url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=600) as resp, tmp.open("wb") as fh:  # nosec B310 - URL prevalidated as HTTPS by _require_https_url
            while True:
                chunk = resp.read(1 << 20)
                if not chunk:
                    break
                fh.write(chunk)
    except HTTPError as exc:
        tmp.unlink(missing_ok=True)  # F2: .part nunca queda huérfano; dest previo intacto
        raise CorpusAcquisitionError(f"HTTP {exc.code} descargando {dest.name}") from exc
    except (URLError, TimeoutError, OSError) as exc:
        tmp.unlink(missing_ok=True)
        raise CorpusAcquisitionError(f"red falló ({type(exc).__name__}) descargando {dest.name}") from exc
    size = tmp.stat().st_size
    if expected_size is not None and size != expected_size:
        tmp.unlink(missing_ok=True)
        raise CorpusValidationError(f"{dest.name}: tamaño {size} != publicado {expected_size}")
    tmp.replace(dest)


def image_info(path: Path) -> dict[str, Any]:
    with Image.open(path) as im:
        mode = im.mode
        return {"width": im.width, "height": im.height, "mode": mode}


def height_bit_depth(mode: str) -> int:
    """8 para L/P/I, 16 para I;16, 32 para F/I de EXR (no usado hoy)."""
    if mode in ("I;16", "I;16B", "I;16L", "I;16N"):
        return 16
    if mode == "I":
        return 16
    if mode == "F":
        return 32
    return 8


def _ph_files(asset_id: str) -> dict[str, Any]:
    payload: dict[str, Any] = json.loads(_http_get(PH_FILES_API.format(asset_id=asset_id)).decode("utf-8"))
    return payload


def _existing_source_entry(source_path: Path, *, build_only: bool) -> dict[str, Any] | None:
    """--build-only: reutiliza el source.json previo (offline, timestamps estables)."""
    if not build_only or not source_path.exists():
        return None
    try:
        cached: dict[str, Any] = json.loads(source_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return cached


def polyhaven_entry(asset_id: str, family: str, root: Path, *, build_only: bool) -> dict[str, Any]:
    """Descarga nor_gl 1k + Displacement 1k del asset y valida md5 publicado (§10)."""
    out_dir = root / "originals" / "polyhaven" / asset_id
    out_dir.mkdir(parents=True, exist_ok=True)
    source_path = out_dir / "source.json"
    cached = _existing_source_entry(source_path, build_only=build_only)
    if cached is not None:
        return cached
    files = _ph_files(asset_id)
    try:
        normal_spec = files["nor_gl"]["1k"]["png"]
        height_spec = files["Displacement"]["1k"]["png"]
    except KeyError as exc:  # pragma: no cover - depende del upstream
        raise CorpusValidationError(f"{asset_id}: falta nor_gl/Displacement 1k png upstream ({exc})") from exc
    normal_path = out_dir / f"{asset_id}_nor_gl_1k.png"
    height_path = out_dir / f"{asset_id}_disp_1k.png"
    if not build_only:
        for spec, dest in ((normal_spec, normal_path), (height_spec, height_path)):
            if dest.exists() and dest.stat().st_size == spec["size"] and md5_file(dest) == spec["md5"]:
                continue  # ya descargado y verificado: no re-descargar
            _http_download(spec["url"], dest, expected_size=spec["size"])
    for spec, dest in ((normal_spec, normal_path), (height_spec, height_path)):
        if not dest.exists():
            raise CorpusValidationError(f"{asset_id}: falta {dest.name} (¿--build-only sin descarga previa?)")
        if dest.stat().st_size != spec["size"]:
            raise CorpusValidationError(f"{asset_id}: {dest.name} tamaño distinto al publicado")
        if md5_file(dest) != spec["md5"]:
            raise CorpusValidationError(f"{asset_id}: {dest.name} md5 != md5 oficial upstream (§13)")
    n_info, h_info = image_info(normal_path), image_info(height_path)
    if (n_info["width"], n_info["height"]) != (h_info["width"], h_info["height"]):
        raise CorpusValidationError(f"{asset_id}: resolución normal != height (§14)")
    timestamp = _timestamp_of(source_path)
    return {
        "asset_id": f"polyhaven_{asset_id}",
        "provider": "polyhaven",
        "family": family,
        "material_name": asset_id.replace("_", " ").title(),
        "official_asset_page": PH_ASSET_PAGE.format(asset_id=asset_id),
        "official_download_url_or_identifier": normal_spec["url"],
        "official_source": PH_ASSET_PAGE.format(asset_id=asset_id),
        "license": LICENSE_ID,
        "license_reference": PH_LICENSE_URL,
        "license_verification": "platform-wide CC0 (polyhaven.com/license); asset page sin licencia distinta",
        "normal_filename_original": normal_path.name,
        "height_filename_original": height_path.name,
        "normal_path": str(normal_path),
        "height_path": str(height_path),
        "normal_sha256": sha256_file(normal_path),
        "height_sha256": sha256_file(height_path),
        "normal_size_bytes": normal_path.stat().st_size,
        "height_size_bytes": height_path.stat().st_size,
        "normal_resolution": [n_info["width"], n_info["height"]],
        "height_resolution": [h_info["width"], h_info["height"]],
        "normal_format": "PNG",
        "height_format": "PNG",
        "height_bit_depth": height_bit_depth(h_info["mode"]),
        "normal_convention": "OPENGL",
        "height_semantics": (
            "RELATIVE_GRAYSCALE_DISPLACEMENT: mapa 'Displacement' oficial (escala absoluta no documentada); "
            "EXR 16-bit disponible upstream pero fuera del pipeline Pillow — se usa el PNG lossless sin transformar"
        ),
        "download_timestamp": timestamp,
        "provenance_status": "OFFICIAL_HASH_VERIFIED",
        "official_hash": {
            "algorithm": "md5",
            "normal": normal_spec["md5"],
            "height": height_spec["md5"],
            "scope": "per-file (API files/<id>)",
            "verified": True,
        },
        "notes": "1k PNG lossless; bytes upstream sin transformar",
    }


def ambientcg_entry(asset_id: str, family: str, root: Path, *, build_only: bool) -> dict[str, Any]:
    """Descarga el zip 1K-PNG oficial y extrae NormalGL + Displacement sin tocarlos."""
    out_dir = root / "originals" / "ambientcg" / asset_id
    out_dir.mkdir(parents=True, exist_ok=True)
    dl_dir = out_dir / "download"
    dl_dir.mkdir(parents=True, exist_ok=True)
    source_path = out_dir / "source.json"
    cached = _existing_source_entry(source_path, build_only=build_only)
    if cached is not None:
        return cached
    api = json.loads(_http_get(ACG_API.format(asset_id=asset_id)).decode("utf-8"))
    found = api.get("foundAssets") or []
    if not found:
        raise CorpusValidationError(f"{asset_id}: no existe en la API oficial")
    asset = found[0]
    if "displacement" not in (asset.get("maps") or []):
        raise CorpusValidationError(f"{asset_id}: el upstream no ofrece displacement")
    downloads = (
        asset.get("downloadFolders", {})
        .get("default", {})
        .get("downloadFiletypeCategories", {})
        .get("zip", {})
        .get("downloads", [])
    )
    one_k = next((d for d in downloads if d.get("attribute") == "1K-PNG"), None)
    if one_k is None:
        raise CorpusValidationError(f"{asset_id}: sin zip 1K-PNG oficial")
    zip_path = dl_dir / one_k["fileName"]
    if not build_only and (not zip_path.exists() or zip_path.stat().st_size != one_k["size"]):
        _http_download(one_k["downloadLink"], zip_path, expected_size=one_k["size"])
    if not zip_path.exists():
        raise CorpusValidationError(f"{asset_id}: falta {zip_path.name}")
    names = {"normal": f"{asset_id}_1K-PNG_NormalGL.png", "height": f"{asset_id}_1K-PNG_Displacement.png"}
    with zipfile.ZipFile(zip_path) as zf:
        archivos = set(zf.namelist())
        for name in names.values():
            if name not in archivos:
                raise CorpusValidationError(f"{asset_id}: el zip no contiene {name}")
    normal_path, height_path = out_dir / names["normal"], out_dir / names["height"]
    if not build_only or not normal_path.exists() or not height_path.exists():
        with zipfile.ZipFile(zip_path) as zf:
            zf.extract(names["normal"], out_dir)
            zf.extract(names["height"], out_dir)
    n_info, h_info = image_info(normal_path), image_info(height_path)
    if (n_info["width"], n_info["height"]) != (h_info["width"], h_info["height"]):
        raise CorpusValidationError(f"{asset_id}: resolución normal != height (§14)")
    timestamp = _timestamp_of(source_path)
    return {
        "asset_id": f"ambientcg_{asset_id}",
        "provider": "ambientcg",
        "family": family,
        "material_name": asset.get("displayName") or asset_id,
        "official_asset_page": asset.get("shortLink") or ACG_ASSET_PAGE.format(asset_id=asset_id),
        "official_download_url_or_identifier": one_k["downloadLink"],
        "official_source": asset.get("shortLink") or ACG_ASSET_PAGE.format(asset_id=asset_id),
        "license": LICENSE_ID,
        "license_reference": ACG_LICENSE_URL,
        "license_verification": "platform-wide CC0 1.0 (docs.ambientcg.com/license/); asset page sin licencia distinta",
        "normal_filename_original": normal_path.name,
        "height_filename_original": height_path.name,
        "normal_path": str(normal_path),
        "height_path": str(height_path),
        "normal_sha256": sha256_file(normal_path),
        "height_sha256": sha256_file(height_path),
        "normal_size_bytes": normal_path.stat().st_size,
        "height_size_bytes": height_path.stat().st_size,
        "normal_resolution": [n_info["width"], n_info["height"]],
        "height_resolution": [h_info["width"], h_info["height"]],
        "normal_format": "PNG",
        "height_format": "PNG",
        "height_bit_depth": height_bit_depth(h_info["mode"]),
        "normal_convention": "OPENGL",
        "height_semantics": (
            "RELATIVE_GRAYSCALE_DISPLACEMENT: mapa 'Displacement' oficial 16-bit (escala absoluta no documentada)"
        ),
        "download_timestamp": timestamp,
        "provenance_status": "PRIMARY_SOURCE_DOWNLOADED",
        "official_hash": None,
        "container": {
            "kind": "zip",
            "filename": zip_path.name,
            "download_url_stable": one_k["downloadLink"],
            "sha256": sha256_file(zip_path),
            "size_bytes": zip_path.stat().st_size,
            "note": "redirect del CDN oficial a un path con token transitorio NO registrado aquí",
        },
        "notes": "zip 1K-PNG oficial preservado completo en download/; extraídos sólo NormalGL y Displacement sin transformar",
    }


def _timestamp_of(source_path: Path) -> str:
    if source_path.exists():
        try:
            previo: dict[str, Any] = json.loads(source_path.read_text(encoding="utf-8"))
            return str(previo.get("download_timestamp", _now()))
        except (json.JSONDecodeError, OSError):
            pass
    return _now()


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def validate_entry(entry: dict[str, Any]) -> None:
    """§14/§27 — valida presencia, hashes, par y procedencia admisible."""
    for key in REQUIRED_ENTRY_FIELDS:
        if entry.get(key) in (None, "", []):
            raise CorpusValidationError(f"{entry.get('asset_id', '?')}: falta {key}")
    if entry["provenance_status"] not in VALID_PROVENANCE:
        raise CorpusValidationError(f"{entry['asset_id']}: provenance {entry['provenance_status']!r} no admitida")
    for key in ("normal_sha256", "height_sha256"):
        value = entry[key]
        if not isinstance(value, str) or len(value) != 64:
            raise CorpusValidationError(f"{entry['asset_id']}: {key} inválido")
    if list(entry["normal_resolution"]) != list(entry["height_resolution"]):
        raise CorpusValidationError(f"{entry['asset_id']}: par normal/height con resolución distinta (§14)")
    # Gate de forma pre-registrado: el pipeline resuelve a 512² con resize cuadrado;
    # una textura no cuadrada se "aplastaría" (aspect ratio perdido). Todo el corpus
    # M2 era cuadrado, así que restringimos Cohort A al mismo régimen (§17: formato
    # utilizable, decidido ANTES de ver cualquier reconstrucción).
    if entry["normal_resolution"][0] != entry["normal_resolution"][1]:
        raise CorpusValidationError(f"{entry['asset_id']}: textura no cuadrada {entry['normal_resolution']}")
    # F3: Cohort A exige convención DECLARADA (§19). UNKNOWN queda para metadata
    # histórica (corpus M2), jamás entrada ejecutable; el límite más temprano es aquí.
    if entry["normal_convention"] not in ("OPENGL", "DIRECTX"):
        raise CorpusValidationError(
            f"{entry['asset_id']}: normal_convention {entry['normal_convention']!r} — "
            "Cohort A requiere OPENGL o DIRECTX declaradas"
        )


def verify_local_files(entry: dict[str, Any]) -> None:
    """§43 — los bits locales deben seguir coincidiendo con el manifest."""
    for path_key, hash_key, size_key in (
        ("normal_path", "normal_sha256", "normal_size_bytes"),
        ("height_path", "height_sha256", "height_size_bytes"),
    ):
        path = Path(entry[path_key])
        if not path.exists():
            raise CorpusValidationError(f"{entry['asset_id']}: falta {path}")
        if path.stat().st_size != entry[size_key]:
            raise CorpusValidationError(f"{entry['asset_id']}: {path.name} cambió de tamaño")
        if sha256_file(path) != entry[hash_key]:
            raise CorpusValidationError(f"{entry['asset_id']}: {path.name} sha256 != manifest (§43)")


def split_of_family(family: str) -> str:
    """Split pre-registrado §18 (idéntico al de EXP-M2: mismas familias calibración)."""
    return split_of(family)


def build_manifest(entries: list[dict[str, Any]], root: Path) -> dict[str, Any]:
    """Valida el set completo y construye el manifest §10/§21 con split congelado."""
    seen: set[str] = set()
    for entry in entries:
        validate_entry(entry)
        verify_local_files(entry)
        if entry["asset_id"] in seen:
            raise CorpusValidationError(f"asset duplicado: {entry['asset_id']} (§27)")
        seen.add(entry["asset_id"])
    split_by_asset = {entry["asset_id"]: split_of_family(entry["family"]) for entry in entries}
    calibration = {aid for aid, split in split_by_asset.items() if split == "CALIBRATION"}
    held_out = {aid for aid, split in split_by_asset.items() if split == "HELD_OUT"}
    overlap = calibration & held_out
    if overlap:  # imposible por construcción, pero queda anclado (§27)
        raise CorpusValidationError(f"overlap CALIBRATION/HELD_OUT: {sorted(overlap)}")
    families = sorted({entry["family"] for entry in entries})
    providers = sorted({entry["provider"] for entry in entries})
    counts: dict[str, int] = {}
    for entry in entries:
        counts[entry["provenance_status"]] = counts.get(entry["provenance_status"], 0) + 1
    return {
        "dataset": "EXP-M3 clean authored corpus (primary sources)",
        "cohort_a": {
            "status": "READY",
            "data_root": str(root),
            "assets": entries,
            "families": families,
            "providers": providers,
            "provenance_counts": counts,
            "split_by_asset": split_by_asset,
            "split_rule": "pre-registrado en fetch_exp_m2_corpus.CALIBRATION_FAMILIES (brick/wood_planks/rock)",
            "acquisition": {
                "primary_sources_only": True,
                "mirrors_used": False,
                "binaries_in_git": False,
                "download_timestamp_utc": _now(),
            },
            "attempt_trail": [
                {
                    "source": "ambientCG + Poly Haven + MatSynth (sandbox previo)",
                    "result": "BLOQUEADO por red del sandbox (EXP-M3 Attempt 1) → EXP_M3_DATA_INSUFFICIENT",
                },
                {
                    "source": "ambientCG (API v2) + Poly Haven (API files)",
                    "result": f"Attempt 2 en estación del operador: {len(entries)} assets / {len(families)} familias",
                },
            ],
        },
        "cohort_b": {
            "label": "EVALUATION_DIAGNOSTIC_ONLY",
            "source_manifest": "exp-m2-authored-manifest.json",
            "source_manifest_sha256": "4d00501949fc006d39a10ba148b269a35c080a92c9e367a4d5a2b7b029d941cb",
            "provenance": "MIRROR_ONLY (explícitamente NO evidencia principal; diagnóstico únicamente)",
            "filter": "height_std >= 2/255 AND oracle_agreement_deg < {20,30,40} (sensibilidad)",
        },
    }


def write_source_json(entry: dict[str, Any], root: Path) -> None:
    asset_dir = Path(entry["normal_path"]).parent
    asset_dir.mkdir(parents=True, exist_ok=True)
    (asset_dir / "source.json").write_text(json.dumps(entry, indent=2) + "\n", encoding="utf-8")


def acquire(root: Path, *, only: str | None, build_only: bool) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    if only in (None, "polyhaven"):
        for asset_id, family in POLYHAVEN_ASSETS.items():
            entry = polyhaven_entry(asset_id, family, root, build_only=build_only)
            write_source_json(entry, root)
            entries.append(entry)
            print(f"PH  {entry['asset_id']:<28} {entry['provenance_status']:<24} {entry['normal_resolution']}")
    if only in (None, "ambientcg"):
        for asset_id, family in AMBIENTCG_ASSETS.items():
            entry = ambientcg_entry(asset_id, family, root, build_only=build_only)
            write_source_json(entry, root)
            entries.append(entry)
            print(f"ACG {entry['asset_id']:<28} {entry['provenance_status']:<24} {entry['normal_resolution']}")
    return entries


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--only", choices=("polyhaven", "ambientcg"), default=None)
    parser.add_argument("--build-only", action="store_true", help="sin red: reconstruye desde originals/")
    args = parser.parse_args()
    entries = acquire(args.data_root, only=args.only, build_only=args.build_only)
    manifest = build_manifest(entries, args.data_root)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    n, fams = len(entries), manifest["cohort_a"]["families"]
    print(f"\nmanifest: {args.out} | assets={n} | familias={len(fams)} {fams}")
    print(f"provenance: {manifest['cohort_a']['provenance_counts']}")
    print(f"sha256(manifest): {sha256_file(args.out)}")


if __name__ == "__main__":
    try:
        main()
    except CorpusValidationError as exc:
        print(f"CORPUS INVALIDO: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc

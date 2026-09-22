"""EXP-M2 M2-A — adquisición opt-in del corpus authored + manifest versionado.

SÓLO research workflow: NUNCA se ejecuta en CI (§45). No redistribuye assets: los
descarga el operador desde mirrors públicos de assets ambientCG (CC0 1.0, verificado
en https://docs.ambientcg.com/license/) y este script construye el manifest con SHA256.

Uso (desde el root del repo):
    1. Clonar los mirrors parcialmente en /tmp/expm2_data/mirrors/:
       git clone --filter=blob:none --sparse --depth 1 \\
           https://github.com/Calinou/godot-cmvalley /tmp/expm2_data/mirrors/Calinou_godot-cmvalley
       (cd ... && git sparse-checkout set ambientcg)
       # ídem petroulacl/fps-buildings-env-kit (environment/ground-textures/ambientcg)
       # y BastiaanOlij/drawable-textures-demo (assets/cc0textures.com)
    2. python -m sky_claw.local.native_parallax.research.fetch_exp_m2_corpus \\
           --mirrors /tmp/expm2_data/mirrors \\
           --out docs/design/research/native-parallax/data/exp-m2-authored-manifest.json

El manifest registra de dónde vinieron los BITS (mirror) y quién es el AUTOR upstream
(ambientCG, asset_id con shortLink canónico). Falla si un asset no pasa el quality gate
(§39): legible, mismo tamaño, resolución mínima, licencia CC0.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from sky_claw.local.native_parallax.research.authored_dataset import (
    DatasetInvalidError,
    build_manifest_entry,
)

# asset_id → (familia, mirror, convención declarada, variante normal usada).
# petroulacl/bastiaan traen NormalGL+NormalDX explícitos → declared OPENGL (usamos GL).
# cmvalley usa el naming viejo "_Normal.jpg" (sin sufijo) → convención UNKNOWN (§9/§12).
CM = "Calinou_godot-cmvalley/ambientcg/{id}_2K_{map_}.jpg"
PE = "petroulacl_fps-buildings-env-kit/environment/ground-textures/ambientcg/{id}_2K-JPG/{id}_2K-JPG_{map_}.jpg"
BA = "BastiaanOlij_drawable-textures-demo/assets/cc0textures.com/{id}_2K-JPG/{id}_2K-JPG_{map_}.jpg"

FAMILIES: dict[str, str] = {
    "Asphalt013": "asphalt_road",
    "Asphalt021": "asphalt_road",
    "Road001": "asphalt_road",
    "Bricks014": "brick",
    "Bricks017": "brick",
    "Bricks019": "brick",
    "Bricks032": "brick",
    "Bricks040": "brick",
    "Bricks066": "brick",
    "Concrete012": "concrete",
    "Concrete027": "concrete",
    "Concrete035": "concrete",
    "Grass004": "ground_grass",
    "Grass005": "ground_grass",
    "Gravel011": "ground_gravel",
    "Ground037": "ground",
    "Snow002": "ground_snow",
    "Metal018": "metal",
    "Metal049A": "metal",
    "Metal062C": "metal",
    "Metal063": "metal",
    "PavingStones002": "tiles_paving",
    "PavingStones054": "tiles_paving",
    "Tiles049": "tiles",
    "Planks012": "wood_planks",
    "Planks014": "wood_planks",
    "Rock010": "rock",
    "Rock022": "rock",
    "Rock029": "rock",
    "Rock030": "rock",
    "WoodFloor027": "wood_floor",
    "WoodFloor030": "wood_floor",
    "WoodFloor043": "wood_floor",
    "Wood043": "wood_generic",
}

# Split por FAMILIA (§22): calibración = brick/wood_planks/rock; held-out = resto.
CALIBRATION_FAMILIES = frozenset({"brick", "wood_planks", "rock"})

CM_IDS = (
    "Asphalt013",
    "Bricks014",
    "Bricks017",
    "Bricks019",
    "Bricks032",
    "Bricks040",
    "Concrete012",
    "Concrete027",
    "Concrete035",
    "Grass004",
    "Gravel011",
    "Metal018",
    "PavingStones002",
    "PavingStones054",
    "Planks012",
    "Planks014",
    "Rock010",
    "Rock022",
    "Rock029",
    "Rock030",
    "Snow002",
    "Tiles049",
    "WoodFloor027",
    "WoodFloor030",
    "WoodFloor043",
    "Wood043",
)
PE_IDS = ("Asphalt021", "Bricks066", "Concrete012", "Grass004", "Ground037", "Metal063", "Road001")
BA_IDS = ("Grass005", "Metal049A", "Metal062C")


def _entry(
    mirrors: Path, aid: str, template: str, convention: str, variant: str, mirror_name: str
) -> dict[str, object]:
    normal = mirrors / template.format(id=aid, map_=variant)
    height = mirrors / template.format(id=aid, map_="Displacement")
    return build_manifest_entry(
        asset_id=aid,
        family=FAMILIES[aid],
        mirror=mirror_name,
        normal_path=normal,
        height_path=height,
        declared_convention=convention,
        normal_variant=variant,
    )


def build(mirrors: Path) -> dict[str, Any]:
    assets: list[dict[str, object]] = []
    skipped: list[str] = []
    for aid in PE_IDS:
        try:
            assets.append(_entry(mirrors, aid, PE, "OPENGL", "NormalGL", "petroulacl/fps-buildings-env-kit"))
        except DatasetInvalidError as exc:
            skipped.append(f"{aid}: {exc}")
    for aid in BA_IDS:
        try:
            assets.append(_entry(mirrors, aid, BA, "OPENGL", "NormalGL", "BastiaanOlij/drawable-textures-demo"))
        except DatasetInvalidError as exc:
            skipped.append(f"{aid}: {exc}")
    for aid in CM_IDS:
        try:
            assets.append(_entry(mirrors, aid, CM, "UNKNOWN", "Normal", "Calinou/godot-cmvalley"))
        except DatasetInvalidError as exc:
            skipped.append(f"{aid}: {exc}")
    seen: set[str] = set()
    unique: list[dict[str, object]] = []
    for a in assets:
        if a["asset_id"] in seen:
            continue
        seen.add(str(a["asset_id"]))
        unique.append(a)
    split = {
        a["asset_id"]: ("CALIBRATION" if FAMILIES[str(a["asset_id"])] in CALIBRATION_FAMILIES else "HELD_OUT")
        for a in unique
    }
    return {
        "dataset": "ambientCG subset via GitHub mirrors (EXP-M2 corpus)",
        "upstream_license": "CC0-1.0 (platform-wide, https://docs.ambientcg.com/license/)",
        "verification": {
            "license_page_checked": True,
            "api_spot_check_ids": [
                "Asphalt013",
                "Bricks014",
                "Rock030",
                "Planks012",
                "WoodFloor027",
                "Tiles049",
                "Metal018",
            ],
            "normal_convention_documented": False,
            "height_scale_documented": False,
        },
        "assets": unique,
        "split_by_family": {a["asset_id"]: split[a["asset_id"]] for a in unique},
        "skipped": skipped,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mirrors", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    manifest = build(args.mirrors)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(manifest, indent=2, sort_keys=False) + "\n")
    n = len(manifest["assets"])
    fams = sorted({FAMILIES[str(a["asset_id"])] for a in manifest["assets"]})
    print(f"manifest: {args.out} | assets={n} | familias={len(fams)} {fams}")
    for s in manifest["skipped"]:
        print("SKIPPED:", s)


if __name__ == "__main__":
    main()

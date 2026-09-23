"""EXP-M3 — tests de adquisición/validación del corpus primario (§27).

Sin red: los fixtures son PNG chicos en tmp_path. Cubren el contrato que separa
Cohort A (evidencia principal) de mirrors: procedencia admisible, source/id
presentes, hash mismatch, par mismatch, duplicados y split sin solapamiento.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from sky_claw.local.native_parallax.research.fetch_exp_m3_primary_corpus import (
    CorpusValidationError,
    build_manifest,
    sha256_file,
    split_of_family,
    validate_entry,
    verify_local_files,
)


def _make_pair(tmp_path: Path, asset_id: str, *, size: tuple[int, int] = (16, 16)) -> tuple[Path, Path]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    normal = tmp_path / f"{asset_id}_n.png"
    height = tmp_path / f"{asset_id}_h.png"
    Image.new("RGB", size, (128, 128, 255)).save(normal)
    Image.new("L", size, 128).save(height)
    return normal, height


def _entry(tmp_path: Path, asset_id: str = "polyhaven_x", family: str = "brick", **overrides: object) -> dict:
    normal, height = _make_pair(tmp_path, asset_id)
    entry: dict = {
        "asset_id": asset_id,
        "provider": "polyhaven",
        "family": family,
        "material_name": "X",
        "official_asset_page": "https://example.invalid/a/x",
        "official_download_url_or_identifier": "https://example.invalid/a/x/n.png",
        "official_source": "https://example.invalid/a/x",
        "license": "CC0-1.0",
        "license_reference": "https://example.invalid/license",
        "license_verification": "test",
        "normal_filename_original": normal.name,
        "height_filename_original": height.name,
        "normal_path": str(normal),
        "height_path": str(height),
        "normal_sha256": sha256_file(normal),
        "height_sha256": sha256_file(height),
        "normal_size_bytes": normal.stat().st_size,
        "height_size_bytes": height.stat().st_size,
        "normal_resolution": [16, 16],
        "height_resolution": [16, 16],
        "normal_format": "PNG",
        "height_format": "PNG",
        "height_bit_depth": 8,
        "normal_convention": "OPENGL",
        "height_semantics": "TEST",
        "download_timestamp": "2026-09-23T00:00:00Z",
        "provenance_status": "PRIMARY_SOURCE_DOWNLOADED",
        "official_hash": None,
        "notes": "",
    }
    entry.update(overrides)
    return entry


# ---------------------------------------------------------------- §9/§27 procedencia


def test_primary_source_downloaded_entra_y_mirror_solo_no(tmp_path: Path) -> None:
    ok = _entry(tmp_path / "a", "a")
    mirror = _entry(tmp_path / "b", "b", provenance_status="MIRROR_ONLY")
    mirror_hash = _entry(tmp_path / "c", "c", provenance_status="MIRROR_HASH_VERIFIED")
    validate_entry(ok)  # no raise
    for bad in (mirror, mirror_hash):
        with pytest.raises(CorpusValidationError, match="no admitida"):
            validate_entry(bad)


def test_entry_sin_source_url_o_asset_id_falla(tmp_path: Path) -> None:
    for key in ("official_source", "official_asset_page", "asset_id"):
        entry = _entry(tmp_path / key, f"a_{key}")
        entry[key] = ""
        with pytest.raises(CorpusValidationError, match="falta"):
            validate_entry(entry)


# ---------------------------------------------------------------- §14 par válido


def test_pair_resolution_mismatch_falla(tmp_path: Path) -> None:
    entry = _entry(tmp_path, "mismatch")
    entry["height_resolution"] = [16, 8]
    with pytest.raises(CorpusValidationError, match="resolución distinta"):
        validate_entry(entry)


def test_textura_no_cuadrada_falla(tmp_path: Path) -> None:
    entry = _entry(tmp_path, "nonsquare")
    entry["normal_resolution"] = [32, 16]
    entry["height_resolution"] = [32, 16]
    with pytest.raises(CorpusValidationError, match="no cuadrada"):
        validate_entry(entry)


# ---------------------------------------------------------------- §43 hash gate


def test_hash_mismatch_falla(tmp_path: Path) -> None:
    entry = _entry(tmp_path, "hash")
    path = Path(entry["normal_path"])
    data = bytearray(path.read_bytes())
    data[-1] ^= 0xFF  # mismo tamaño, contenido distinto → sólo el hash puede detectarlo
    path.write_bytes(bytes(data))
    with pytest.raises(CorpusValidationError, match="sha256"):
        verify_local_files(entry)


def test_archivo_faltante_falla(tmp_path: Path) -> None:
    entry = _entry(tmp_path, "missing")
    Path(entry["height_path"]).unlink()
    with pytest.raises(CorpusValidationError, match="falta"):
        verify_local_files(entry)


# ---------------------------------------------------------------- §27 manifest completo


def test_duplicate_asset_id_falla(tmp_path: Path) -> None:
    entries = [_entry(tmp_path, "dup", "brick"), _entry(tmp_path, "dup", "rock")]
    with pytest.raises(CorpusValidationError, match="duplicado"):
        build_manifest(entries, tmp_path)


def test_split_cubre_todo_sin_solapamiento(tmp_path: Path) -> None:
    entries = [
        _entry(tmp_path, "b1", "brick"),
        _entry(tmp_path, "t1", "tiles"),
        _entry(tmp_path, "g1", "ground"),
    ]
    manifest = build_manifest(entries, tmp_path)
    split = manifest["cohort_a"]["split_by_asset"]
    calibration = {aid for aid, s in split.items() if s == "CALIBRATION"}
    held_out = {aid for aid, s in split.items() if s == "HELD_OUT"}
    assert calibration & held_out == set()
    assert calibration | held_out == {"b1", "t1", "g1"}
    assert calibration == {"b1"}  # brick ∈ CALIBRATION_FAMILIES pre-registradas
    assert held_out == {"t1", "g1"}


def test_split_rule_es_la_de_exp_m2() -> None:
    assert split_of_family("brick") == "CALIBRATION"
    assert split_of_family("wood_planks") == "CALIBRATION"
    assert split_of_family("rock") == "CALIBRATION"
    assert split_of_family("tiles") == "HELD_OUT"
    assert split_of_family("stone") == "HELD_OUT"


# ---------------------------------------------------------------- decoder 16-bit (§7)


def test_decode_height_16bit_preserva_rango_completo(tmp_path: Path) -> None:
    """Fix EXP-M3: convert('L') sobre I;16 saturaba a 255 (height constante)."""
    from sky_claw.local.native_parallax.research.authored_dataset import decode_height_image

    arr = np.linspace(0, 65535, 64, dtype=np.uint16).reshape(8, 8)
    path = tmp_path / "h16.png"
    Image.fromarray(arr).save(path)
    decoded = decode_height_image(path)
    assert decoded.max() == pytest.approx(1.0)
    assert decoded.min() == pytest.approx(0.0)
    assert float(decoded.std()) > 0.2  # información preservada, no una constante


def test_decode_height_8bit_sin_cambios(tmp_path: Path) -> None:
    from sky_claw.local.native_parallax.research.authored_dataset import decode_height_image

    arr = np.linspace(0, 255, 64, dtype=np.uint8).reshape(8, 8)
    path = tmp_path / "h8.png"
    Image.fromarray(arr).save(path)
    decoded = decode_height_image(path)
    assert decoded.max() == pytest.approx(1.0)
    assert decoded.min() == pytest.approx(0.0)

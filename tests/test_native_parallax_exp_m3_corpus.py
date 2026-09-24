"""EXP-M3 — tests de adquisición/validación del corpus primario (§27).

Sin red: los fixtures son PNG chicos en tmp_path. Cubren el contrato que separa
Cohort A (evidencia principal) de mirrors: procedencia admisible, source/id
presentes, hash mismatch, par mismatch, duplicados y split sin solapamiento.
"""

from __future__ import annotations

import io
import urllib.request
from pathlib import Path
from urllib.error import HTTPError, URLError

import numpy as np
import pytest
from PIL import Image

from sky_claw.local.native_parallax.research.authored_dataset import (
    DatasetInvalidError,
    decode_height_image,
)
from sky_claw.local.native_parallax.research.fetch_exp_m3_primary_corpus import (
    CorpusAcquisitionError,
    CorpusValidationError,
    _http_download,
    _http_get,
    build_manifest,
    height_bit_depth,
    md5_file,
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


# ---------------------------------------------------------------- F1: height modo F


def _save_f(path: Path, arr: np.ndarray) -> None:
    Image.fromarray(arr.astype(np.float32), mode="F").save(path, format="TIFF")


def test_f1_height_bit_depth_promete_y_decode_cumple(tmp_path: Path) -> None:
    assert height_bit_depth("F") == 32
    arr = np.linspace(0.2, 0.8, 64, dtype=np.float32).reshape(8, 8)
    p = tmp_path / "h.tif"
    _save_f(p, arr)
    out = decode_height_image(p)
    np.testing.assert_allclose(out, arr, rtol=1e-6)  # sin pasar por uint8


def test_f1_float_fuera_de_rango_normaliza_lineal_documentada(tmp_path: Path) -> None:
    arr = np.linspace(-3.0, 5.0, 64, dtype=np.float32).reshape(8, 8)
    p = tmp_path / "h.tif"
    _save_f(p, arr)
    out = decode_height_image(p)
    assert float(out.min()) >= 0.0 and float(out.max()) <= 1.0
    np.testing.assert_allclose(float(out.min()), 0.0, atol=1e-6)
    np.testing.assert_allclose(float(out.max()), 1.0, atol=1e-6)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_f1_float_nan_inf_fail_closed(tmp_path: Path, bad: float) -> None:
    arr = np.full((8, 8), 0.5, dtype=np.float32)
    arr[0, 0] = bad
    p = tmp_path / "h.tif"
    _save_f(p, arr)
    with pytest.raises(DatasetInvalidError, match="NaN/Inf"):
        decode_height_image(p)


def test_f1_float_degenerado_fail_closed(tmp_path: Path) -> None:
    p = tmp_path / "h.tif"
    _save_f(p, np.full((8, 8), 7.0, dtype=np.float32))  # rango nulo fuera de [0,1]
    with pytest.raises(DatasetInvalidError, match="degenerado"):
        decode_height_image(p)


# ---------------------------------------------------------------- F2: errores HTTP


def _fake_urlopen(payload: bytes | Exception):
    def fake(url: object, timeout: float | None = None) -> object:
        if isinstance(payload, Exception):
            raise payload
        return io.BytesIO(payload)

    return fake


def test_f2_http_get_404_preserva_status_y_chaining(monkeypatch: pytest.MonkeyPatch) -> None:
    err = HTTPError("https://host/x?sig=SECRETO", 404, "Not Found", None, io.BytesIO(b""))
    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen(err))
    with pytest.raises(CorpusAcquisitionError, match="HTTP 404") as excinfo:
        _http_get("https://host/x?sig=SECRETO")
    assert isinstance(excinfo.value.__cause__, HTTPError)
    assert "SECRETO" not in str(excinfo.value)  # sin query firmada en el mensaje


def test_f2_http_get_urlerror_clasificado(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen(URLError("conexion rechazada")))
    with pytest.raises(CorpusAcquisitionError, match="URLError"):
        _http_get("https://host/x")


def test_f2_download_fallido_limpia_part_y_preserva_dest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dest = tmp_path / "asset.zip"
    dest.write_bytes(b"ORIGINAL-VALIDO")
    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen(URLError("corte a mitad")))
    with pytest.raises(CorpusAcquisitionError):
        _http_download("https://host/a.zip", dest)
    assert not (tmp_path / "asset.zip.part").exists()
    assert dest.read_bytes() == b"ORIGINAL-VALIDO"


def test_f2_download_size_mismatch_limpia_part(monkeypatch: pytest.MonkeyPatch) -> None:
    dest = Path("/tmp") / "nunca-escrito.zip"
    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen(b"poquito"))
    with pytest.raises(CorpusValidationError, match="tamaño"):
        _http_download("https://host/a.zip", dest, expected_size=12345)
    assert not dest.with_suffix(".zip.part").exists()


# ---------------------------------------------------------------- F3: UNKNOWN en Cohort A


def _valid_entry() -> dict[str, object]:
    return {
        "asset_id": "X1",
        "family": "brick",
        "provider": "p",
        "official_asset_page": "https://p/x",
        "official_download_url_or_identifier": "https://p/x.zip",
        "official_source": "https://p/x",
        "license": "CC0-1.0",
        "license_reference": "https://p/license",
        "normal_filename_original": "n_orig.png",
        "height_filename_original": "h_orig.png",
        "normal_path": "n.png",
        "height_path": "h.png",
        "normal_sha256": "0" * 64,
        "height_sha256": "1" * 64,
        "normal_size_bytes": 1,
        "height_size_bytes": 1,
        "normal_resolution": [1024, 1024],
        "height_resolution": [1024, 1024],
        "provenance_status": "PRIMARY_SOURCE_DOWNLOADED",
        "normal_convention": "OPENGL",
        "height_semantics": "RELATIVE_0_1",
        "normal_format": "PNG",
        "height_format": "PNG",
        "height_bit_depth": 16,
        "download_timestamp": "2026-09-23T00:00:00Z",
    }


def test_f3_validate_entry_rechaza_unknown_en_el_limite_mas_temprano() -> None:
    entry = _valid_entry()
    entry["normal_convention"] = "UNKNOWN"
    with pytest.raises(CorpusValidationError, match="OPENGL o DIRECTX"):
        validate_entry(entry)


def test_f3_validate_entry_acepta_directx_y_opengl() -> None:
    for conv in ("OPENGL", "DIRECTX"):
        entry = _valid_entry()
        entry["normal_convention"] = conv
        validate_entry(entry)  # no raise


# ------------------------------------------ F6: harden de adquisición (SAST B310/B324)


def test_f6_https_valida_llega_a_urlopen(monkeypatch: pytest.MonkeyPatch) -> None:
    """La URL https válida atraviesa _require_https_url sin mutaciones."""
    vistas: list[str] = []

    def fake(url: object, timeout: float | None = None) -> object:
        assert isinstance(url, urllib.request.Request)
        vistas.append(str(url.full_url))
        return io.BytesIO(b"payload")

    monkeypatch.setattr(urllib.request, "urlopen", fake)
    assert _http_get("https://polyhaven.com/a?sig=SECRETO") == b"payload"
    assert vistas == ["https://polyhaven.com/a?sig=SECRETO"]


@pytest.mark.parametrize(
    "url_mala",
    [
        "http://host/a.zip",  # clear-text
        "file:///etc/passwd",  # recurso local
        "ftp://host/a.zip",  # ftp
        "custom://host/a.zip",  # esquema desconocido
        "https:///sin-host",  # sin hostname
        "https://[invalid",  # inparseable
        "https://user:pass@host/a.zip",  # userinfo embebido
    ],
)
def test_f6_urls_invalidas_rechazadas_antes_de_urlopen(
    url_mala: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def prohibido(url: object, timeout: float | None = None) -> object:
        raise AssertionError("urlopen NO debe invocarse para URLs inválidas")

    monkeypatch.setattr(urllib.request, "urlopen", prohibido)
    with pytest.raises(CorpusValidationError):
        _http_get(url_mala)
    with pytest.raises(CorpusValidationError):
        _http_download(url_mala, tmp_path / "a.zip")
    assert not (tmp_path / "a.zip").exists()
    assert not (tmp_path / "a.zip.part").exists()


def test_f6_userinfo_embebido_nunca_en_el_mensaje() -> None:
    """El mensaje de error nombra el host, jamás las credenciales embebidas."""
    with pytest.raises(CorpusValidationError) as excinfo:
        _http_get("https://user:secreto@host/a.zip")
    assert "secreto" not in str(excinfo.value)
    assert "host" in str(excinfo.value)


def test_f6_md5_provenance_digest_conocido(tmp_path: Path) -> None:
    """md5_file es transparente a usedforsecurity=False: digest estable para bytes conocidos."""
    muestra = tmp_path / "muestra.bin"
    muestra.write_bytes(b"skyclaw-exp-m3-provenance")
    assert md5_file(muestra) == "b68aa2a85a1310fc338fbc564cf735cc"

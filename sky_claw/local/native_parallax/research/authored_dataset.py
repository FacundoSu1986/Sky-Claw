"""EXP-M2 — dataset authored (normal+height) y transformaciones de carga.

CLEAN-ROOM: solo matemática pública + NumPy + Pillow + assets CC0 verificados.
El authored height se usa EXCLUSIVAMENTE como oráculo de evaluación (§11 del brief):
ninguna función de política/trust de este módulo acepta height como argumento.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray
from PIL import Image

RESOLUTION_NOT_INFORMATIVE = "NOT_INFORMATIVE"
IMAGE_SIZE_STRICT = False  # los mirrors deben tener el mismo tamaño lógico; se registra


@dataclass(frozen=True)
class MaterialSpec:
    """Entrada del manifest: identidad, procedencia y licencia de un material."""

    asset_id: str
    family: str
    source: str  # proveedor upstream (authoring pipeline)
    mirror: str  # de dónde se descargaron los bits para esta corrida
    license: str
    license_url: str
    source_url: str
    normal_path: str
    height_path: str
    normal_sha256: str
    height_sha256: str
    native_resolution: tuple[int, int]
    declared_convention: str  # "OPENGL" | "UNKNOWN" (§9: no inferir silenciosamente)
    height_semantics: str
    notes: str = field(default="")


class DatasetInvalidError(Exception):
    """Un asset no pasa el quality gate (§39): no se penaliza al solver."""


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def build_manifest_entry(
    *,
    asset_id: str,
    family: str,
    mirror: str,
    normal_path: Path,
    height_path: Path,
    declared_convention: str,
    normal_variant: str,
) -> dict[str, Any]:
    """Construye una entrada del manifest validando lectura y coherencia (§39)."""
    try:
        with Image.open(normal_path) as im:
            n_size = (im.width, im.height)
            im.load()
        with Image.open(height_path) as im:
            h_size = (im.width, im.height)
            im.load()
    except OSError as exc:
        raise DatasetInvalidError(f"{asset_id}: archivo ileible/faltante ({exc})") from exc
    if n_size != h_size:
        raise DatasetInvalidError(f"{asset_id}: resolución normal {n_size} != height {h_size}")
    if n_size[0] < 64 or n_size[1] < 64:
        raise DatasetInvalidError(f"{asset_id}: resolución demasiado baja {n_size}")
    return {
        "asset_id": asset_id,
        "family": family,
        "source": "ambientCG",
        "mirror": mirror,
        "license": "CC0-1.0",
        "license_url": "https://docs.ambientcg.com/license/",
        "source_url": f"https://ambientcg.com/a/{asset_id}",
        "normal_path": str(normal_path),
        "height_path": str(height_path),
        "normal_sha256": _sha256(normal_path),
        "height_sha256": _sha256(height_path),
        "native_resolution": list(n_size),
        "declared_convention": declared_convention,
        "normal_variant": normal_variant,
        # docs.ambientcg.com no documenta convención ni rango del height (§9):
        "height_semantics": "UNKNOWN_SCALE_RELATIVE_GRAYSCALE",
        "license_verification": "platform-wide CC0 1.0 (docs.ambientcg.com/license/)",
        "notes": "",
    }


def load_manifest(path: Path) -> list[MaterialSpec]:
    """Carga y valida el manifest. SIN licencia verificada no entra al corpus (M5)."""
    raw = json.loads(path.read_text())
    specs: list[MaterialSpec] = []
    seen: set[str] = set()
    for entry in raw["assets"]:
        aid = entry["asset_id"]
        if aid in seen:
            raise DatasetInvalidError(f"asset duplicado: {aid}")
        seen.add(aid)
        if entry.get("license") != "CC0-1.0":
            raise DatasetInvalidError(f"{aid}: licencia no verificada CC0 ({entry.get('license')!r})")
        for key in ("normal_sha256", "height_sha256"):
            if len(entry[key]) != 64:
                raise DatasetInvalidError(f"{aid}: {key} inválido")
        if entry.get("declared_convention") not in ("OPENGL", "DIRECTX", "UNKNOWN"):
            raise DatasetInvalidError(f"{aid}: declared_convention inválida (M4)")
        specs.append(
            MaterialSpec(
                asset_id=aid,
                family=entry["family"],
                source=entry["source"],
                mirror=entry["mirror"],
                license=entry["license"],
                license_url=entry["license_url"],
                source_url=entry["source_url"],
                normal_path=entry["normal_path"],
                height_path=entry["height_path"],
                normal_sha256=entry["normal_sha256"],
                height_sha256=entry["height_sha256"],
                native_resolution=(entry["native_resolution"][0], entry["native_resolution"][1]),
                declared_convention=entry["declared_convention"],
                height_semantics=entry["height_semantics"],
                notes=entry.get("notes", ""),
            )
        )
    return specs


def verify_against_manifest(spec: MaterialSpec) -> None:
    """Falla si los bits locales ya no coinciden con el manifest (§43 hash gate)."""
    for path, expected in (
        (Path(spec.normal_path), spec.normal_sha256),
        (Path(spec.height_path), spec.height_sha256),
    ):
        if not path.exists():
            raise DatasetInvalidError(f"{spec.asset_id}: falta {path}")
        if _sha256(path) != expected:
            raise DatasetInvalidError(f"{spec.asset_id}: hash cambió para {path} (§43)")


def apply_convention(normal: NDArray[np.float64], declared: str, tested: str) -> NDArray[np.float64]:
    """Aplica flip de Y sólo si hay EVIDENCIA declarada (§12). UNKNOWN no se toca aquí.

    tested solo puede diferir de declared cuando el llamador registra el diagnóstico
    oracle_best_convention; esta función es determinista y sin efectos.
    """
    if declared == tested:
        return normal
    if declared == "UNKNOWN":
        raise ValueError("convention UNKNOWN: el flip debe decidirse por diagnóstico registrado, no aquí")
    out = np.array(normal, dtype=np.float64, copy=True)
    out[..., 1] = -out[..., 1]
    return np.asarray(out, dtype=np.float64)


def decode_normal_image(path: Path) -> NDArray[np.float64]:
    """JPG/PNG tangential-space RGB → normal unitaria float64 en [-1,1]³ (renormalizada)."""
    with Image.open(path) as im:
        rgb = np.asarray(im.convert("RGB"), dtype=np.float64)
    n = rgb / 255.0 * 2.0 - 1.0
    length = np.linalg.norm(n, axis=-1, keepdims=True)
    length = np.maximum(length, 1e-12)
    return np.asarray(n / length, dtype=np.float64)


def unit_length_residual(path: Path) -> NDArray[np.float64]:
    """F: residual de longitud unitaria ANTES de renormalizar (proxy §19-F)."""
    with Image.open(path) as im:
        rgb = np.asarray(im.convert("RGB"), dtype=np.float64)
    n = rgb / 255.0 * 2.0 - 1.0
    return np.asarray(np.abs(np.linalg.norm(n, axis=-1) - 1.0), dtype=np.float64)


def decode_height_image(path: Path) -> NDArray[np.float64]:
    """Height/displacement relativo → float64 [0,1] (escala/offset ambiguos: los maneja el oráculo).

    8-bit (L/P/RGB/RGBA) se normaliza por 255; 16-bit (I;16*) por 65535. Fix EXP-M3:
    la versión previa asumía 8-bit y ``convert("L")`` sobre un PNG de 16 bits saturaba
    a 255 (Poly Haven) o clipeaba (ambientCG), destruyendo el height — invisible con
    el corpus JPG 8-bit de EXP-M2. El formato 16-bit es el preferido por el brief.
    """
    with Image.open(path) as im:
        mode = im.mode
        if mode in ("I;16", "I;16B", "I;16L", "I;16N"):
            gray16 = np.asarray(im, dtype=np.float64)
            return np.asarray(gray16 / 65535.0, dtype=np.float64)
        if mode == "I":
            arr = np.asarray(im, dtype=np.float64)
            peak = float(arr.max()) if arr.size else 0.0
            if peak <= 255.0:
                return np.asarray(arr / 255.0, dtype=np.float64)
            if peak <= 65535.0:
                return np.asarray(arr / 65535.0, dtype=np.float64)
            raise DatasetInvalidError(f"{path}: height modo I con rango >16-bit no soportado")
        gray = np.asarray(im.convert("L"), dtype=np.float64)
        return np.asarray(gray / 255.0, dtype=np.float64)


def resize_height(h: NDArray[np.float64], size: int) -> NDArray[np.float64]:
    """Downsample height: interpolación lineal escalar (§13)."""
    if h.shape[0] == size and h.shape[1] == size:
        return np.asarray(h, dtype=np.float64)
    im = Image.fromarray((np.clip(h, 0.0, 1.0) * 255.0).astype(np.uint8), mode="L")
    out = im.resize((size, size), Image.Resampling.BILINEAR)
    return np.asarray(np.asarray(out, dtype=np.float64) / 255.0, dtype=np.float64)


def resize_normal(n: NDArray[np.float64], size: int) -> NDArray[np.float64]:
    """Downsample vector-aware (§13): canal por canal + RENORMALIZACIÓN (M3).

    Redimensionar RGB como imagen ordinaria sin renormalizar queda prohibido y
    testado: la norma debe permanecer ≈1 tras el resize.
    """
    if n.shape[0] == size and n.shape[1] == size:
        return np.asarray(n, dtype=np.float64)
    channels = []
    for c in range(3):
        im = Image.fromarray((np.clip(n[..., c], -1.0, 1.0) * 127.5 + 127.5).astype(np.uint8), mode="L")
        channels.append(
            (np.asarray(im.resize((size, size), Image.Resampling.BILINEAR), dtype=np.float64) - 127.5) / 127.5
        )
    out = np.stack(channels, axis=-1)
    length = np.maximum(np.linalg.norm(out, axis=-1, keepdims=True), 1e-12)
    return np.asarray(out / length, dtype=np.float64)


def two_channel_q8_proxy(normal: NDArray[np.float64]) -> dict[str, Any]:
    """TWO_CHANNEL_Q8_PROXY (§30, NO 'BC5 simulation').

    Cuantiza XY a 8 bits y reconstruye z = sqrt(max(0, 1-x²-y²)) SIN renormalizar,
    separando explícitamente (§29, M6):
      - xy_radicand_invalid_fraction: x²+y²>1 (píxel XY fuera del disco)
      - reconstructed_z_zero_fraction: z reconstruido == 0 exacto
      - negative_nz_fraction: nz del ORIGINAL < 0 (normal real apuntando atrás)
    """
    q = np.clip(np.rint((normal[..., 0:2] * 0.5 + 0.5) * 255.0), 0, 255) / 255.0 * 2.0 - 1.0
    radicand = 1.0 - q[..., 0] ** 2 - q[..., 1] ** 2
    z = np.sqrt(np.maximum(radicand, 0.0))
    return {
        "two_channel_proxy_rmse": float(np.sqrt(np.mean((np.stack([q[..., 0], q[..., 1], z], -1) - normal) ** 2))),
        "xy_radicand_invalid_fraction": float(np.mean(radicand < 0.0)),
        "reconstructed_z_zero_fraction": float(np.mean(z == 0.0)),
        "negative_nz_fraction": float(np.mean(normal[..., 2] < 0.0)),
    }


def edge_seam_ratio(arr: NDArray[np.float64]) -> dict[str, Any]:
    """Caracterización de periodicidad (§14): discontinuidad de borde vs referencia interior.

    ratio >~ 3 sugiere NON_PERIODIC / BAD_BOUNDARY; <=~2 LIKELY_TILEABLE.
    Se separa por filas/columnas (seam vertical y horizontal).
    """

    def _ratio(a: NDArray[np.float64], axis: int) -> float:
        edge = float(np.mean(np.abs(np.take(a, 0, axis=axis) - np.take(a, a.shape[axis] - 1, axis=axis))))
        interior = float(np.mean(np.abs(np.diff(a, axis=axis))))
        if interior < 1e-12:
            return RESOLUTION_NOT_INFORMATIVE if isinstance(interior, str) else 0.0 if edge < 1e-12 else 999.0
        return float(edge / interior)

    return {"seam_x_ratio": _ratio(arr, 1), "seam_y_ratio": _ratio(arr, 0)}


@dataclass
class AuthoredMaterial:
    """Material ya decodificado a resolución de investigación."""

    spec: MaterialSpec
    tested_convention: str
    normal: NDArray[np.float64]
    height: NDArray[np.float64]  # SOLO para oráculo; las políticas no lo reciben

    @property
    def nz(self) -> NDArray[np.float64]:
        return np.asarray(self.normal[..., 2], dtype=np.float64)


def load_asset(
    spec: MaterialSpec,
    resolution: int,
    *,
    tested_convention: str | None = None,
) -> AuthoredMaterial:
    """Carga normal+height con verificación de hash y resize documentado (§13/§46)."""
    verify_against_manifest(spec)
    tested = tested_convention or spec.declared_convention
    normal = decode_normal_image(Path(spec.normal_path))
    if spec.declared_convention in ("OPENGL", "DIRECTX") and tested != spec.declared_convention:
        normal = apply_convention(normal, spec.declared_convention, tested)
    height = decode_height_image(Path(spec.height_path))
    return AuthoredMaterial(
        spec=spec,
        tested_convention=tested,
        normal=resize_normal(normal, resolution),
        height=resize_height(height, resolution),
    )

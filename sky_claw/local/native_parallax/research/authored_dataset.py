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


class InvalidNormalShapeError(DatasetInvalidError):
    """Envolvente estructural inválida de una normal (contrato #717 §3.4, hallazgo F1).

    Hereda de `DatasetInvalidError` por compatibilidad de los `except` existentes, pero
    es una señal **distinta**: dice "esto no es una normal tangencial", no "la dirección
    no es confiable". Un consumidor que quiera tratar sólo la degeneración captura
    `DegenerateNormalError` explícitamente.

    Casos: `ndim != 3`, `shape[-1] != 3`, `H == 0`, `W == 0`, `size` no entero o `< 1`,
    cualquier NaN/Inf en la entrada. Ninguno se difiere por V-03 (que cubre **sólo**
    `H != W`, y sigue `DEFERRED`).
    """

    def __init__(self, message: str, *, campo: str, valor: object) -> None:
        super().__init__(message)
        self.campo = campo
        self.valor = valor


class DegenerateNormalError(DatasetInvalidError):
    """Degeneración H4: la interpolación canceló o casi canceló las direcciones (D-06).

    No es una exclusión de integridad: el asset **no desaparece** del roster, se declara
    `NON_EVALUABLE` con censo explícito. Mientras no exista roster persistente (Slice 3),
    el consumidor **aborta** antes de publicar un resultado científico — `continue` está
    prohibido para esta señal (§5.3.4 del contrato #717).
    """

    def __init__(
        self,
        *,
        asset_id: str | None,
        degenerate_count: int,
        total_pixels: int,
        threshold: float,
        fraction: float,
        reason: str,
    ) -> None:
        super().__init__(
            f"NON_EVALUABLE[{asset_id or '<sin identidad>'}]: {reason} — "
            f"{degenerate_count}/{total_pixels} píxeles con 0 <= L < {threshold} "
            f"(fracción {fraction:.6g})"
        )
        self.asset_id = asset_id
        self.degenerate_count = degenerate_count
        self.total_pixels = total_pixels
        self.threshold = threshold
        self.fraction = fraction
        self.reason = reason


# --- H4: umbral de degeneración (contrato #717 §6.1, R7) ---------------------
# `DEGENERATE_THRESHOLD` = 5/16, derivado de teoría + controles sintéticos. NO se
# calibró con corpus real ni con resultados históricos (prohibido por §6.1).
#
# 1. Cota de cancelación. `v = suma_i w_i n_i` con `w_i >= 0`, `suma w_i = 1`,
#    `||n_i|| = 1`  =>  `L = ||v||` en [0, 1]. El ínfimo 0 se ALCANZA: una entrada
#    de normales unitarias alternadas `(+-1,0,0)` es cancelada exactamente por el
#    kernel separable a factor 2 (el de reducción 1024->512), y también por los de
#    factor 4/8/16 — verificado por barrido de factor, fase y borde. La conjetura
#    "Nyquist es el peor caso" deja de ser conjetura: es una cota alcanzada, y como
#    `L >= 0` siempre, ninguna configuración soportada puede bajarla. El barrido
#    corrobora `min L = 0` y muestra que el recorte del kernel en el borde es MÁS
#    benigno (no cancela del todo).
#
# 2. Criterio de confiabilidad (fijado ANTES de medir). Una dirección es confiable
#    sólo si la resuelve el DATO, no el último bit del dato. La procedencia declarada
#    de la entrada (§7.1 del preflight) es una retícula de 8 bits con paso
#    `q = 2/255`; cerca de la normal plana su cuanto angular es `q_ang = q rad`.
#    Se perturba cada píxel de entrada dentro de su propia celda de retícula
#    (`U(-q/2, q/2)^3`, renormalizado) y se exige que el cambio de dirección
#    inducido cumpla `delta_theta(L) <= q_ang` para todo `L' >= L`.
#
# 3. Umbral. La amplificación de una perturbación es `delta_theta ~ ||dv|| / L`; para
#    el kernel separable a factor 2 (pesos 1-D `[1/8, 3/8, 3/8, 1/8]`) la propagación
#    RMS de la perturbación vale `||dv|| = sqrt(sum_k w_k^2) * q = 5/16 * q`. Igualando
#    `delta_theta = q_ang = q` queda `L = 5/16`. Medición independiente sobre la
#    familia sintética de `L` analítico: el cruce cae en `L = 0.309 <= 0.3125`, así
#    que la cota analítica no se viola y el umbral la redondea hacia arriba.
#
# 4. Dominio y riesgo residual. Para todo factor `k >= 2` la cota decrece
#    (0.3125 -> 0.2346 -> 0.1641 -> ...), así que `5/16` es CONSERVADOR en las
#    reducciones soportadas por el pipeline (corpus 1024/2048 -> 512, k en {2, 4}).
#    Para `1 < k < 2` la cota es mayor: `RESIDUAL_RISK = DECLARED`, no oculto.
#    Sobre contenido realista (superficie fractal 1/f con pendientes hasta 2.5,
#    1024->512) el porcentaje de píxeles por debajo del umbral es 0.0000 %; el
#    contenido que sí cae es cancelación genuina (Nyquist: 100 %; período 5: 40 %;
#    ruido blanco: 59 %).
DEGENERATE_THRESHOLD = 5.0 / 16.0


@dataclass(frozen=True)
class NormalResizeResult:
    """Vehículo de la señal H4 (§5.3.1 del contrato #717, hallazgo F2).

    La firma vigente `resize_normal -> NDArray` no tiene por dónde transportar
    `degenerate_count`, `total_pixels` ni `threshold`. Este resultado tipado es el
    vehículo: quien necesita el censo —y puede decidir con la identidad del asset—
    usa `resize_normal_diagnostic`.
    """

    array: NDArray[np.float64]
    degenerate_count: int
    total_pixels: int
    threshold: float
    degenerate: bool


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
        if mode == "F":
            # Contrato height float32 (find F1): sin convertir a "L" (truncaría a uint8).
            arr = np.asarray(im, dtype=np.float64)
            if not np.all(np.isfinite(arr)):
                raise DatasetInvalidError(f"{path}: height modo F contiene NaN/Inf (fail-closed)")
            lo, hi = float(arr.min()), float(arr.max())
            if lo >= 0.0 and hi <= 1.0:
                return np.asarray(arr, dtype=np.float64)
            # Fuera de [0,1]: normalización lineal global min-max, DOCUMENTADA e
            # invariante para la evaluación (el oráculo fitea afín global, §11/§38).
            span = hi - lo
            if span <= 0.0:
                raise DatasetInvalidError(f"{path}: height modo F degenerado (rango nulo)")
            return np.asarray((arr - lo) / span, dtype=np.float64)
        gray = np.asarray(im.convert("L"), dtype=np.float64)
        return np.asarray(gray / 255.0, dtype=np.float64)


def resize_height(h: NDArray[np.float64], size: int) -> NDArray[np.float64]:
    """Resize scalar heights bilinearly, preserving float/16-bit height information.

    On resize, values are clipped to [0,1] and passed through Pillow mode F
    (float32), avoiding uint8 quantization. The result is returned as float64.
    """
    if h.shape[0] == size and h.shape[1] == size:
        return np.asarray(h, dtype=np.float64)
    im = Image.fromarray(np.asarray(np.clip(h, 0.0, 1.0), dtype=np.float32))
    out = im.resize((size, size), Image.Resampling.BILINEAR)
    return np.asarray(out, dtype=np.float64)


def _validar_envolvente_normal(n: object, size: object) -> NDArray[np.float64]:
    """Envolvente estructural de la normal (contrato #717 §3.4, cierre de F1).

    Corre **antes** de la bifurcación identidad/resize: el camino identidad de la
    implementación histórica era un bypass total de la validación (devolvía `1000.0`
    sin clip, `NaN` sin marcar y `(H,W,4)` descartando el 4.º canal en silencio).
    """
    if type(size) is not int or size < 1:
        raise InvalidNormalShapeError(
            f"size debe ser un entero >= 1 (no bool); recibido {size!r}", campo="size", valor=size
        )
    try:
        arr = np.asarray(n, dtype=np.float64)
    except (TypeError, ValueError) as exc:
        raise InvalidNormalShapeError(
            f"la normal no es convertible a float64: {exc}", campo="dtype", valor=type(n).__name__
        ) from exc
    if arr.ndim != 3:
        raise InvalidNormalShapeError(
            f"ndim debe ser exactamente 3 (H, W, 3); recibido {arr.ndim}", campo="ndim", valor=arr.ndim
        )
    if arr.shape[-1] != 3:
        raise InvalidNormalShapeError(
            f"shape[-1] debe ser exactamente 3; recibido {arr.shape[-1]} (no se descarta ningún canal)",
            campo="shape[-1]",
            valor=arr.shape[-1],
        )
    if arr.shape[0] < 1 or arr.shape[1] < 1:
        raise InvalidNormalShapeError(
            f"H y W deben ser > 0; recibido {arr.shape[:2]}", campo="shape", valor=arr.shape[:2]
        )
    if not bool(np.all(np.isfinite(arr))):
        raise InvalidNormalShapeError(
            "la normal contiene NaN/Inf (V-04: fail-closed, NO se clipea en silencio)",
            campo="finitud",
            valor="no_finito",
        )
    return arr


def resize_normal_diagnostic(n: NDArray[np.float64], size: int) -> NormalResizeResult:
    """Resize vector-aware con evidencia: devuelve `NormalResizeResult`, NO lanza por degeneración.

    Corrección de H4 respecto del histórico (`uint8` intermedio + `out / max(L, 1e-12)`):

    1. **Envolvente estructural** (§3.4): `ndim == 3`, `shape[-1] == 3`, `H, W > 0`,
       `size` entero `>= 1`, finitud. Rechazo fail-closed con `InvalidNormalShapeError`.
    2. **Finitud** (V-04) y **clip declarado a `[-1,1]`** (§3.4.1 paso 3): el rango es
       POSTCONDICIÓN, no precondición — se aplica **antes** de la bifurcación, de modo
       que vale en ambos caminos sin excepción.
    3. **Bifurcación**: `H == W == size` es no-op (D-07) sobre entradas admisibles; en
       otro caso se interpola canal por canal en **Pillow modo F** (float32), sin
       cuantización intermedia a `uint8`, con `BILINEAR` explícito.
    4. **Renormalización con política de degenerados** (D-06): si algún píxel tiene
       `0 <= L < DEGENERATE_THRESHOLD` —donde `L` es la norma del vector **interpolado,
       antes de renormalizar**— el resultado se marca degenerado y **no se renormaliza**
       (renormalizar una dirección casi nula la inventa). `array` conserva entonces el
       vector crudo: la decisión es del llamador, que conoce la identidad del asset.

    La fachada `resize_normal` consume este resultado y **lanza** ante degeneración; el
    array crudo nunca se publica como normal.
    """
    arr = _validar_envolvente_normal(n, size)
    arr = np.clip(arr, -1.0, 1.0)

    total_pixels = size * size
    if arr.shape[0] == size and arr.shape[1] == size:
        # D-07: no-op del paso de remuestreo para entradas admisibles — sin interpolar
        # y sin renormalizar (`decode_normal_image` ya renormalizó aguas arriba).
        return NormalResizeResult(
            array=arr,
            degenerate_count=0,
            total_pixels=total_pixels,
            threshold=DEGENERATE_THRESHOLD,
            degenerate=False,
        )

    canales: list[NDArray[np.float64]] = []
    for c in range(3):
        im = Image.fromarray(np.ascontiguousarray(arr[..., c], dtype=np.float32))
        canales.append(np.asarray(im.resize((size, size), Image.Resampling.BILINEAR), dtype=np.float64))
    crudo = np.asarray(np.stack(canales, axis=-1), dtype=np.float64)

    largo = np.linalg.norm(crudo, axis=-1)
    degenerados = int(np.count_nonzero(largo < DEGENERATE_THRESHOLD))
    if degenerados:
        return NormalResizeResult(
            array=crudo,
            degenerate_count=degenerados,
            total_pixels=total_pixels,
            threshold=DEGENERATE_THRESHOLD,
            degenerate=True,
        )
    # `largo >= DEGENERATE_THRESHOLD > 0` en todo píxel: la división es segura y no hace
    # falta el piso `max(L, 1e-12)` que el histórico usaba como anti-ZeroDivision y que
    # no garantizaba norma unitaria (D-2 del preflight).
    return NormalResizeResult(
        array=np.asarray(crudo / largo[..., None], dtype=np.float64),
        degenerate_count=0,
        total_pixels=total_pixels,
        threshold=DEGENERATE_THRESHOLD,
        degenerate=False,
    )


def resize_normal(n: NDArray[np.float64], size: int) -> NDArray[np.float64]:
    """Downsample vector-aware (§13): canal por canal + RENORMALIZACIÓN (M3).

    Fachada fail-closed (RC-1 del contrato #717): delega en `resize_normal_diagnostic`,
    inspecciona el resultado y **lanza `DegenerateNormalError`** si hay degeneración.
    Devuelve `NDArray` exclusivamente para entradas evaluables — el tipo de retorno y la
    aridad se conservan, lo que NO se conserva es la disposición a devolver un array
    degenerado en silencio.

    Un llamador que necesite el array crudo *y* el censo usa `resize_normal_diagnostic`,
    que es explícito por nombre.
    """
    resultado = resize_normal_diagnostic(n, size)
    if resultado.degenerate:
        raise DegenerateNormalError(
            asset_id=None,  # la fachada no recibe el `spec`: identidad degradada a propósito
            degenerate_count=resultado.degenerate_count,
            total_pixels=resultado.total_pixels,
            threshold=resultado.threshold,
            fraction=resultado.degenerate_count / resultado.total_pixels,
            reason="uso directo de la fachada sobre campo degenerado",
        )
    return resultado.array


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
    """Carga normal+height con verificación de hash y resize documentado (§13/§46).

    La degeneración H4 se decide **con la identidad del asset** (§5.3.1 del contrato
    #717): el resultado del resizer se inspecciona **antes** de construir
    `AuthoredMaterial`, de modo que un array con direcciones no confiables nunca llega
    al solver. El error viaja en el objeto de excepción, no dentro del array.
    """
    verify_against_manifest(spec)
    tested = tested_convention or spec.declared_convention
    normal = decode_normal_image(Path(spec.normal_path))
    if spec.declared_convention in ("OPENGL", "DIRECTX") and tested != spec.declared_convention:
        normal = apply_convention(normal, spec.declared_convention, tested)
    height = decode_height_image(Path(spec.height_path))

    resultado = resize_normal_diagnostic(normal, resolution)
    if resultado.degenerate:
        raise DegenerateNormalError(
            asset_id=spec.asset_id,
            degenerate_count=resultado.degenerate_count,
            total_pixels=resultado.total_pixels,
            threshold=resultado.threshold,
            fraction=resultado.degenerate_count / resultado.total_pixels,
            reason="vector nulo/casi-nulo tras interpolación",
        )
    return AuthoredMaterial(
        spec=spec,
        tested_convention=tested,
        normal=resultado.array,
        height=resize_height(height, resolution),
    )

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
    cualquier NaN/Inf en la entrada y —F01, adjudicado como R2-lite— un píxel de entrada
    de norma no unitaria que no requirió clip (`campo="norma_de_entrada"`, `valor` = cuántos).
    Ninguno se difiere por V-03 (que cubre **sólo** `H != W`, y sigue `DEFERRED`).
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
#    factor 4/8/16. La premisa "Nyquist es el peor caso" NO queda probada por esto: lo
#    que el barrido establece es el ALCANCE del ínfimo —que `0` se alcanza, y como
#    `L >= 0` siempre, ninguna configuración soportada puede bajarlo—. Es una falsación
#    INTENTADA y no lograda, que es exactamente el estado `CONJECTURE_NOT_FALSIFIED`
#    que fija el gate (§6.1, N-12); no lo asciende. El barrido ejecutable (factor
#    `k in {2,4,8,16}` x fase `fase in [0, k)` x borde) corrobora `min L = 0` en TODAS
#    las combinaciones. Evidencia registrada como `R7_NYQUIST_ATTAINMENT` (punto 4).
#
#    **Corrección de una afirmación previa no medida (H4-F02).** El comentario decía
#    que "el recorte del kernel en el borde es MÁS benigno (no cancela del todo)".
#    El barrido lo FALSIFICA: el borde alcanza el MISMO ínfimo que el interior
#    (`min L <= 1e-12`, a precisión de máquina, en las 30 combinaciones medidas),
#    así que no es más benigno.
#    La afirmación se retira; lo único que se sostiene es que el borde NO baja el
#    mínimo, es decir que no agrega un modo de cancelación peor.
#    H4-F02-ANCLA-BORDE: el borde no es más benigno — alcanza el mismo ínfimo.
#
#    OJO: esto acota el MÍNIMO de `L` —cuán degenerado puede llegar a estar el
#    campo—, NO el umbral de rechazo; ver punto 4.
#
# 2. Criterio de confiabilidad (fijado ANTES de medir). Una dirección es confiable
#    sólo si la resuelve el DATO, no el último bit del dato. La procedencia declarada
#    de la entrada (§7.1 del preflight) es una retícula de 8 bits con paso
#    `q = 2/255`; cerca de la normal plana su cuanto angular es `q_ang = q rad`.
#    Se perturba cada píxel de entrada dentro de su propia celda de retícula
#    (`U(-q/2, q/2)^3`, renormalizado) y se exige que el cambio de dirección
#    inducido cumpla `delta_theta(L) <= q_ang` para todo `L' >= L`.
#
# 3. Amplificación, y de dónde sale `5/16` (CORREGIDO — hallazgo H4-F02).
#    La amplificación de una perturbación es `delta_theta ~ ||dv|| / L`. Para el
#    kernel separable a factor 2 (pesos 1-D `[1/8, 3/8, 3/8, 1/8]`) la suma de
#    cuadrados de los pesos vale `sum_k w_k^2 = 1/64 + 9/64 + 9/64 + 1/64 = 20/64 = 5/16`.
#    Igualar `delta_theta = q_ang = q` con ese factor da `L = 5/16`.
#
#    **`5/16` es `sum_k w_k^2`, NO `sqrt(sum_k w_k^2)`.** La justificación previa
#    escribía `||dv|| = sqrt(sum_k w_k^2) * q = 5/16 * q`, que confunde las dos
#    cantidades: `sqrt(sum_k w_k^2) = sqrt(5/16) = 0.5590`, no `0.3125`.
#
# 4. Estado de la derivación: HEURÍSTICA, no garantía (hallazgo H4-F02).
#    El factor `5/16` sale de una propagación **RMS** (valor medio cuadrático) de la
#    perturbación, y un RMS **no** es una cota superior de error angular: es el
#    promedio de una familia, no el peor caso. La cota *worst-case* de la MISMA celda
#    declarada es `||dv|| <= sum_{k,l} w_k w_l * (q/2) * sqrt(3) = (q/2) * sqrt(3)`,
#    es decir `2.771x` el factor RMS. Por lo tanto:
#
#        R7_NUMERIC_DERIVATION = BLOCKED (la justificación publicada no sostiene una
#                                garantía matemática universal; es una heurística)
#        R7_THRESHOLD_KIND     = HEURISTIC (RMS), NO garantía matemática
#        R7_THRESHOLD_VALUE    = 5/16 (CONSERVADO; prohibido recalibrarlo con corpus)
#        R7_WORST_CASE_BOUND   = (q/2) * sqrt(3) = 2.771x el factor RMS declarado
#        R7_WORST_CASE_PREMISE = CONJECTURE_NOT_FALSIFIED (ESTADO FIJADO por el gate
#                                #717 §6.1, que está FROZEN: N-12 prohíbe presentar
#                                la premisa como resultado. Un comentario de
#                                producción NO reescribe el estado declarado de un
#                                contrato congelado; ver el token de evidencia abajo.)
#        R7_NYQUIST_ATTAINMENT  = ESTABLISHED_AS_ATTAINED_BOUND (evidencia NUEVA de este
#                                Slice, registrada bajo su PROPIO token y no como
#                                reescritura de la premisa: el barrido factor x fase x
#                                borde alcanza min L = 0 en las 30 combinaciones, y
#                                como L >= 0 nada puede bajarlo. Es un hallazgo de
#                                ALCANCE del ínfimo —falsación intentada y no lograda—,
#                                coherente con `CONJECTURE_NOT_FALSIFIED`, NO un
#                                ascenso del gate.)
#        R7_WORST_CASE_CROSSING = 1/2 (evidencia NUEVA del slice de RECONCILIACIÓN #711/
#                                #717/#723; NO cambia el valor del umbral). Bajo una
#                                perturbación COHERENTE perpendicular —la MISMA `(q/2)`
#                                en TODOS los píxeles, en un eje sin componente en el
#                                vector interpolado— la amplificación NO se promedia: los
#                                pesos del kernel son positivos y suman. Medido sobre la
#                                implementación real (Pillow modo F) en k in {2,4,8,16}:
#                                `delta_theta(L) = atan((q/2)/L)`, que cruza `q_ang` en
#                                `L = 1/2` EXACTO. Consecuencia: la banda `[5/16, 1/2)`
#                                —que este umbral declara EVALUABLE— tiene error de
#                                dirección de hasta `1.60x q_ang` en `L = 5/16`. El RMS
#                                (0.0925 en k=2) NO acota ese peor caso; el adversario
#                                que la familia G usaba para cubrirlo (`d[...,0] =
#                                +-(q/2)*s`, alineado con el patrón alternado) mide
#                                `delta_theta == 0` EXACTO y por eso era VACUO. Es la
#                                contraparte en CRUCE de `R7_WORST_CASE_BOUND = 2.771x`;
#                                no autoriza recalibrar: `R7_NUMERIC_DERIVATION` sigue
#                                `BLOCKED` y la decisión es del Tech Lead.
#                                ALCANCE (no leerlo de más): `1/2` es el cruce de ESA familia
#                                de perturbación, NO el peor caso sobre todas las admitidas
#                                por `abs(eps_c) <= q/2` con renormalización — acotar esa
#                                familia exige una cota de error angular en función de la
#                                CORRELACIÓN entre píxeles, que este slice no aporta. Lo
#                                falsado es la PRETENSIÓN DE GARANTÍA, no que `1/2` sea el
#                                valor correcto.
#
#    Lo que SÍ se verifica —con una prueba sintética independiente del valor de
#    producción (oráculo propio, semilla fija, factores `k in {2, 4, 8, 16}`), en
#    `tests/test_native_parallax_h4_resize_normal.py`, familia G— es la
#    CONSERVADURÍA del valor BAJO EL CRITERIO RMS: el cruce medido `delta_theta(L) =
#    q_ang` cae muy por debajo de `5/16` en TODOS los factores soportados. Cruces RMS
#    medidos (malla `linspace(0.02, 0.60, 25)`, paso ~0.0242, así que son cotas
#    SUPERIORES del cruce real):
#    `k=2 -> 0.0925`, `k=4 -> 0.0683`, `k=8 -> 0.0442`, `k=16 -> 0.0200`, todos por
#    debajo de `0.3125`.
#    **Corrección de una segunda afirmación no medida (slice de RECONCILIACIÓN).** El
#    comentario decía que el cruce cae por debajo de `5/16` "tanto bajo el criterio RMS
#    como bajo el peor caso dirigido". Es FALSO: el adversario que lo respaldaba
#    (`d[...,0] = +-(q/2)*s`) está alineado con el patrón alternado y la interpolación lo
#    cancela, de modo que medía `delta_theta == 0` EXACTO y su cruce caía en el primer
#    punto de la grilla. Bajo el peor caso ESTRUCTURAL —perturbación coherente
#    perpendicular— el cruce es `L = 1/2`, `1.6x` el umbral (ver `R7_WORST_CASE_CROSSING`
#    arriba). Se retira la afirmación; `5/16` es conservador sólo bajo el criterio RMS.
#
# 5. Dominio y riesgo residual. `RESIDUAL_RISK = DECLARED`, no oculto. La cota RMS
#    `sum_k w_k^2` DECRECE con el factor `k`, y la secuencia está MEDIDA (derivada del
#    oráculo por respuesta a impulsos, no estimada):
#
#        k=2  -> 0.3125000    k=4  -> 0.1640625    k=8  -> 0.0830078    k=16 -> 0.0416260
#
#    H4-F02-ANCLA-COTA: 0.3125000, 0.1640625, 0.0830078, 0.0416260 para k = 2, 4, 8, 16.
#    (Una versión previa declaraba `0.3125 -> 0.2346 -> 0.1641 -> ...`, que NO es la
#    secuencia medida: el valor `0.2346` no corresponde a ningún factor del kernel.
#    Se corrige con la medición; ver `test_g_la_cota_rms_decrece_con_el_factor`.)
#    Con eso, `5/16` es conservador para las reducciones del pipeline (corpus
#    1024/2048 -> 512, `k` en {2, 4}). Para `1 < k < 2` la cota es mayor y el umbral
#    deja de dominarla.
#    Sobre contenido realista (superficie fractal 1/f con pendientes hasta 2.5,
#    1024->512) el porcentaje de píxeles por debajo del umbral es 0.0000 %; el
#    contenido que sí cae es cancelación genuina (Nyquist: 100 %; período 5: 40 %;
#    ruido blanco: 59 %).
DEGENERATE_THRESHOLD = 5.0 / 16.0

# --- H4: tolerancia de norma del camino identidad (contrato #717 §3.5, D-07) ---
# Fuente SINTÉTICA/ANALÍTICA (§3.5: prohibido ajustarla al corpus). La entrada
# admisible del camino identidad es una normal **unitaria**; el único desvío legítimo
# proviene de la ida/vuelta `float32 -> float64` del pipeline (`Image` modo F), cuya
# cota analítica por componente es `2**-24`, i.e.
# `||v_f32 - v|| <= sqrt(3) * 2**-24 = 1.032e-07`. Se toma `1e-6` (~10x de margen sobre
# esa cota) y se ancla con una medición sintética en
# `tests/test_native_parallax_h4_resize_normal.py` (familia B).
#
# ADJUDICACIÓN H4-F01 — dominio geométrico de la ENTRADA (R2-lite, decisión delegada del
# 2026-10-19, pendiente de ratificación humana antes del merge).
# V-01 (#711) manda RECHAZAR la entrada "geométricamente inválida" y no corregirla por
# renormalización silenciosa, y deja el término sin definir. Se lo define como
#
#     invalida := píxel que NO requirió clip y con |L - 1| > UNIT_NORM_TOLERANCE   (P-ENTRADA)
#
# Es la lectura literal de V-01 y sostiene a la vez, sin enmendar ninguna otra regla cerrada:
#   - §3.2: la salida tiene norma `≈ 1` o `NON_EVALUABLE`, nunca un valor intermedio silencioso;
#   - N-11 (`FUERA_DE_RANGO_FINITO = CLIP, TOLERADO`, CONTRACT_CLOSED): lo que requirió clip es
#     OBSERVABLE y queda exento, por píxel; se recorta y se renormaliza;
#   - D-06: `L < DEGENERATE_THRESHOLD` conserva la precedencia (NON_EVALUABLE) sobre el rechazo
#     de entrada, en los dos caminos;
#   - §5.3.4.7 (simetría de hermanos): identidad y resize aplican la MISMA regla.
# El chequeo es sobre la ENTRADA, nunca sobre el intermediario interpolado, cuya norma no
# unitaria la produce la operación y se renormaliza (D-06). Ojo con el argumento fácil: la
# interpolación NO siempre baja la norma (un campo unitario constante la preserva).
#
# Antes (R1) se renormalizaba cualquier dirección "confiable" (`L >= T`), lo que aceptaba en
# silencio una normal mal decodificada a [0,1] como `(0.5, 0.5, 1.0)`. El pipeline real no
# cambia: `decode_normal_image` ya entrega normales unitarias, así que `load_asset` siempre
# cae en el no-op exacto.
#
#     F01_RESOLUTION         = R2_LITE_IMPLEMENTED (decisión delegada; pendiente de ratificación)
#     F01_CONTRACT_AMENDMENT = PENDING_TECH_LEAD   (el texto de la regla nueva de N-11 —"se
#                              tolera el clip sólo si es observable"— falta ratificarlo)
#
# Matriz de alternativas y evidencia:
# `docs/design/research/native-parallax/h4-resize-normal-contract-reconciliation.md`.
UNIT_NORM_TOLERANCE = 1e-6


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


def _rechazo_por_norma_de_entrada(
    no_unitarios: NDArray[np.bool_], largo_entrada: NDArray[np.float64]
) -> InvalidNormalShapeError:
    """Error de V-01 para una entrada no unitaria que NO requirió clip (F01 = R2-lite)."""
    cuenta = int(np.count_nonzero(no_unitarios))
    peor = float(np.max(np.abs(largo_entrada[no_unitarios] - 1.0)))
    return InvalidNormalShapeError(
        f"{cuenta} píxel(es) de entrada con norma != 1 (desvío máximo {peor:.3g} > "
        f"{UNIT_NORM_TOLERANCE:g}) que no requirieron clip: no es una normal unitaria "
        "(V-01: se rechaza, no se corrige por renormalización silenciosa)",
        campo="norma_de_entrada",
        valor=cuenta,
    )


def resize_normal_diagnostic(n: NDArray[np.float64], size: int) -> NormalResizeResult:
    """Resize vector-aware con evidencia: devuelve `NormalResizeResult`, NO lanza por degeneración.

    Corrección de H4 respecto del histórico (`uint8` intermedio + `out / max(L, 1e-12)`):

    1. **Envolvente estructural** (§3.4): `ndim == 3`, `shape[-1] == 3`, `H, W > 0`,
       `size` entero `>= 1`, finitud. Rechazo fail-closed con `InvalidNormalShapeError`.
    2. **Finitud** (V-04) y **clip declarado a `[-1,1]`** (§3.4.1 paso 3): el rango es
       POSTCONDICIÓN, no precondición — se aplica **antes** de la bifurcación, de modo
       que vale en ambos caminos sin excepción.
    3. **Norma de la ENTRADA** (V-01, F01 adjudicado como R2-lite): un píxel que NO
       requirió clip y cuya norma se desvía de 1 más de `UNIT_NORM_TOLERANCE` se
       RECHAZA con `InvalidNormalShapeError(campo="norma_de_entrada")`; no se corrige
       por renormalización silenciosa. Lo que requirió clip (N-11) queda exento, por
       píxel. `L < DEGENERATE_THRESHOLD` conserva la precedencia (D-06).
    4. **Bifurcación**: `H == W == size` omite el REMUESTREO (D-07), pero **mide la
       norma** y aplica la misma política que el camino `resize`: `degenerate=True` si
       algún píxel cae bajo `DEGENERATE_THRESHOLD`, rechazo de entrada no unitaria,
       no-op exacto si la entrada ya es unitaria y renormalización de lo que requirió
       clip. En otro caso se interpola canal por canal en **Pillow modo F** (float32),
       sin cuantización intermedia a `uint8`, con `BILINEAR` explícito.
    5. **Renormalización con política de degenerados** (D-06): si algún píxel tiene
       `0 <= L < DEGENERATE_THRESHOLD` —donde `L` es la norma del vector **interpolado,
       antes de renormalizar**— el resultado se marca degenerado y **no se renormaliza**
       (renormalizar una dirección casi nula la inventa). `array` conserva entonces el
       vector crudo: la decisión es del llamador, que conoce la identidad del asset.

    La fachada `resize_normal` consume este resultado y **lanza** ante degeneración; el
    array crudo nunca se publica como normal.
    """
    arr = _validar_envolvente_normal(n, size)
    # N-11: lo fuera de rango se recorta y se tolera. Es OBSERVABLE (`requirio_clip`) y por eso
    # es lo único que queda exento del chequeo de norma de la ENTRADA (V-01, F01 = R2-lite).
    requirio_clip = np.any(np.abs(arr) > 1.0, axis=-1)
    arr = np.clip(arr, -1.0, 1.0)
    largo_entrada = np.linalg.norm(arr, axis=-1)
    no_unitarios = ~requirio_clip & (np.abs(largo_entrada - 1.0) > UNIT_NORM_TOLERANCE)

    total_pixels = size * size
    if arr.shape[0] == size and arr.shape[1] == size:
        # D-07: no-op del paso de REMUESTREO (no hay nada que interpolar). Pero la
        # postcondición de salida §3.2 —norma `≈ 1` o `NON_EVALUABLE`, nunca un valor
        # intermedio silencioso— NO tiene excepción de identidad (hallazgo H4-F01): el
        # bypass histórico devolvía el vector nulo, `(1,1,1)` de norma `sqrt(3)`, o un
        # campo de norma 0.5, sin medir una sola norma. La corrección es mínima y
        # SIMÉTRICA con el hermano `resize`: el único paso que se omite es interpolar.
        largo = largo_entrada
        degenerados = int(np.count_nonzero(largo < DEGENERATE_THRESHOLD))
        if degenerados:
            return NormalResizeResult(
                array=arr,
                degenerate_count=degenerados,
                total_pixels=total_pixels,
                threshold=DEGENERATE_THRESHOLD,
                degenerate=True,
            )
        if bool(no_unitarios.any()):
            raise _rechazo_por_norma_de_entrada(no_unitarios, largo_entrada)
        if bool(np.all(np.abs(largo - 1.0) <= UNIT_NORM_TOLERANCE)):
            # Entrada YA unitaria: no-op EXACTO — sin remuestrear, sin copiar y sin
            # renormalizar. Es el caso que `decode_normal_image` produce aguas arriba, y
            # el único para el que D-07 justifica el no-op bit a bit.
            return NormalResizeResult(
                array=arr,
                degenerate_count=0,
                total_pixels=total_pixels,
                threshold=DEGENERATE_THRESHOLD,
                degenerate=False,
            )
        # Sólo llegan acá píxeles que REQUIRIERON clip (el resto no unitario se rechazó arriba):
        # p. ej. `1000.0` lleva a `(1,1,1)`, de norma `sqrt(3)`. Se renormaliza con el MISMO
        # paso que el camino `resize`, en vez de publicar un array cuya norma no es 1. No se
        # inventa dirección — se conserva la de la entrada.
        return NormalResizeResult(
            array=np.asarray(arr / largo[..., None], dtype=np.float64),
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
    if bool(no_unitarios.any()):
        # Mismo rechazo que el camino identidad (hermanos). Va DESPUÉS del degenerado: D-06
        # conserva la precedencia, y el chequeo es sobre la ENTRADA, nunca sobre `crudo`, cuya
        # norma no unitaria la produce la operación y se renormaliza abajo.
        raise _rechazo_por_norma_de_entrada(no_unitarios, largo_entrada)
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

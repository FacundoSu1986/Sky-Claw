"""EXP-M2 — boundary JSON estricto de ``rows.json`` / ``characs.json`` (REPRO-A).

Defecto cerrado: ``run_exp_m2`` serializaba ambos artefactos con ``json.dumps(...)``
en su modo por defecto (``allow_nan=True``), que emite los literales ``NaN`` /
``Infinity`` / ``-Infinity``. No son JSON válido (RFC 8259 §6): ``json.loads`` los
acepta sólo como extensión de Python, y un consumidor estricto (``jq``,
``JSON.parse`` de JavaScript, otro lenguaje) rechaza el artefacto.

Los no finitos NO son hipotéticos en M2 — salen de resultados legítimos:

- ``hf_energy_ratio`` = NaN cuando el height es band-limited (NOT_INFORMATIVE, M7);
- ``r_p01_proxy``/``r_p05_proxy``/``r_min_proxy`` = ±inf en la fila RAW, porque
  σ_eff=0 por definición (``normal_only_features``);
- ``spearman`` = NaN cuando ningún candidato de σ_eff es evaluable (MATH-B.1).

Contrato anclado acá (RED verificado contra ``main`` @ 32e4c1d antes del fix):

- se sanea el ARTEFACTO COMPLETO de forma recursiva —no la clave ``spearman``—:
  no finito → ``null``;
- la escritura usa ``allow_nan=False``: un no finito olvidado FALLA, no se emite;
- los valores finitos y sus tipos no se alteran: este PR cambia la representación
  en el boundary, no el cálculo científico;
- ``winner=None`` (estado NO_EVALUABLE_SIGMA_CANDIDATE) sigue serializándose
  ``null``: el estado no cambia de significado.
"""

from __future__ import annotations

import copy
import json
import re
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from PIL import Image

from sky_claw.local.native_parallax.research import run_exp_m2 as m2
from sky_claw.local.native_parallax.research.authored_dataset import build_manifest_entry
from sky_claw.local.native_parallax.research.normal_from_height import normals_from_gradients, spectral_gradients

# El literal, no la subcadena: ``NaN`` dentro de un nombre de clave futuro no es el bug.
_NAN_LITERAL = re.compile(r"(?<![\w.])NaN(?![\w])")
_INF_LITERAL = re.compile(r"-?Infinity")


def _artifact_con_nan_anidado() -> dict[str, Any]:
    """Artefacto sintético con la FORMA de M2 (brief §8): NaN en dos rutas distintas."""
    return {
        "rows": [
            {
                "hf_energy_ratio": float("nan"),
                "value": 1.25,
            }
        ],
        "sigma_selection": {
            "winner": None,
            "table": [
                {
                    "spearman": float("nan"),
                }
            ],
        },
    }


def _texto_estricto(datos: dict[str, Any]) -> str:
    """Serializa un artefacto YA saneado y falla si algún no finito sobrevivió."""
    return json.dumps(datos, allow_nan=False)


# ---------------------------------------------------------------- sanitizado recursivo


def _finitos_de(obj: Any) -> list[float]:
    """Todos los float (finitos o no) presentes en una estructura anidada."""
    if isinstance(obj, dict):
        return [f for v in obj.values() for f in _finitos_de(v)]
    if isinstance(obj, (list, tuple)):
        return [f for v in obj for f in _finitos_de(v)]
    if isinstance(obj, float):
        return [obj]
    return []


def test_json_safe_convierte_non_finitos_anidados_a_null() -> None:
    """§8/§9: NaN en rutas ANIDADAS y DISTINTAS → ``null`` exacto, no 0 ni "NaN"."""
    saneado = m2._json_safe(_artifact_con_nan_anidado())

    assert saneado == {
        "rows": [{"hf_energy_ratio": None, "value": 1.25}],
        "sigma_selection": {"winner": None, "table": [{"spearman": None}]},
    }
    assert saneado["rows"][0]["hf_energy_ratio"] is None
    assert saneado["sigma_selection"]["table"][0]["spearman"] is None

    texto = _texto_estricto(saneado)
    assert "NaN" not in texto
    assert "Infinity" not in texto


def test_json_safe_cubre_las_tres_formas_no_finitas_en_todas_las_rutas() -> None:
    """M4: sanitizar SÓLO ``sigma_selection.spearman`` deja NaN/±inf en otra ruta.

    El objeto mezcla dict/list/tuple a varias profundidades: el contrato es del
    artefacto completo, no de una clave conocida.
    """
    crudo = {
        "rows": [
            {"hf_energy_ratio": float("inf"), "oracle": {"diagnostics": [float("-inf"), 0.5]}},
            {"hf_energy_ratio": 0.75, "extra": (float("nan"), {"deep": float("nan")})},
        ],
        "sigma_selection": {"table": [{"spearman": float("nan")}]},
    }

    saneado = m2._json_safe(crudo)

    assert saneado["rows"][0]["hf_energy_ratio"] is None
    assert saneado["rows"][0]["oracle"]["diagnostics"] == [None, 0.5]
    assert saneado["rows"][1]["extra"] == [None, {"deep": None}]
    assert saneado["sigma_selection"]["table"][0]["spearman"] is None
    assert sorted(_finitos_de(saneado)) == [0.5, 0.75]
    _texto_estricto(saneado)


def test_json_safe_no_usa_centinelas_numericos_ni_strings() -> None:
    """M5: NaN → 0.0 (o "NaN") sobrevive a un test que sólo mire "es JSON válido"."""
    saneado = m2._json_safe({"nan": float("nan"), "pos": float("inf"), "neg": float("-inf")})

    assert saneado == {"nan": None, "pos": None, "neg": None}
    for clave, valor in saneado.items():
        assert valor is None, clave  # explícito: no 0, no 0.0, no -1, no "NaN"
    assert json.loads(_texto_estricto(saneado)) == {"nan": None, "pos": None, "neg": None}


def test_json_safe_preserva_valores_finitos_y_sus_tipos() -> None:
    """§12/§15: el PR cambia representación en el boundary, NO cálculo ni tipos."""
    crudo: dict[str, Any] = {
        "uno": 1.0,
        "neg": -3.5,
        "cero": 0.0,
        "entero": 7,
        "nulo": None,
        "bool_true": True,
        "bool_false": False,
        "texto": "x",
        "anidado": {"lista": [1.0, -3.5, 0.0, 7, None, True, False, "x"]},
    }
    antes = copy.deepcopy(crudo)

    saneado = m2._json_safe(crudo)

    assert saneado == crudo
    assert type(saneado["uno"]) is float
    assert saneado["neg"] == -3.5
    assert saneado["cero"] == 0.0
    assert type(saneado["entero"]) is int
    assert saneado["nulo"] is None
    assert saneado["bool_true"] is True and saneado["bool_false"] is False
    assert type(saneado["bool_true"]) is bool  # un bool NO es un número a sanear
    assert saneado["texto"] == "x"
    assert saneado["anidado"]["lista"] == crudo["anidado"]["lista"]
    assert crudo == antes  # el sanitizado es PURO: no muta el objeto interno
    _texto_estricto(saneado)


def test_json_safe_maneja_escalares_y_arrays_numpy() -> None:
    """§13: si un np.floating/np.integer llega al boundary, se vuelve JSON-compatible.

    ``np.float64`` ES subclase de ``float`` (json lo serializa, NaN incluido);
    ``np.float32``/``np.int64`` NO lo son y harían fallar ``json.dumps`` con TypeError.
    No finito → ``None``; finito → ``float``/``int`` Python, sin cambiar el valor.
    """
    crudo = {
        "f64_ok": np.float64(1.5),
        "f64_nan": np.float64("nan"),
        "f32_ok": np.float32(-2.25),
        "f32_nan": np.float32("nan"),
        "i64": np.int64(9),
        "i32": np.int32(-4),
        "b_np": np.bool_(True),
        "array": np.asarray([1.0, float("nan"), 3.0]),
        "array_i": np.asarray([1, 2], dtype=np.int64),
    }

    saneado = m2._json_safe(crudo)

    assert saneado["f64_ok"] == 1.5 and type(saneado["f64_ok"]) is float
    assert saneado["f64_nan"] is None
    assert saneado["f32_ok"] == pytest.approx(-2.25) and type(saneado["f32_ok"]) is float
    assert saneado["f32_nan"] is None
    assert saneado["i64"] == 9 and type(saneado["i64"]) is int
    assert saneado["i32"] == -4 and type(saneado["i32"]) is int
    assert saneado["b_np"] is True
    assert saneado["array"] == [1.0, None, 3.0]
    assert saneado["array_i"] == [1, 2]

    texto = _texto_estricto(saneado)
    assert "NaN" not in texto
    assert "Infinity" not in texto


def test_json_safe_no_traga_tipos_desconocidos_en_silencio() -> None:
    """Un objeto arbitrario NO se convierte: ``json.dumps`` debe fallar con TypeError."""

    class Raro:
        pass

    raro = Raro()
    saneado = m2._json_safe({"raro": raro})
    assert saneado["raro"] is raro
    with pytest.raises(TypeError):
        _texto_estricto(saneado)


# ---------------------------------------------------------------- boundary de escritura


def test_write_json_es_estricto_y_sanitiza_el_payload_completo(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """El punto de escritura único de M2: saneado total + ``allow_nan=False``."""
    destino = tmp_path / "artifact.json"
    m2._write_json(
        destino,
        {"a": [float("nan")], "b": {"c": float("inf"), "d": float("-inf"), "e": 1.0}},
    )

    texto = destino.read_text(encoding="utf-8")
    assert "NaN" not in texto and "Infinity" not in texto
    assert json.loads(texto) == {"a": [None], "b": {"c": None, "d": None, "e": 1.0}}

    # Fail-fast: con el sanitizado neutralizado, el no finito NO se emite: explota.
    monkeypatch.setattr(m2, "_json_safe", lambda obj: obj)
    crudo = tmp_path / "crudo.json"
    with pytest.raises(ValueError, match="JSON compliant"):
        m2._write_json(crudo, {"x": float("nan")})
    assert not crudo.exists()


# ---------------------------------------------------------------- corpus sintético (sin corpus real)


def _write_synthetic_asset(root: Path, asset_id: str, n: int = 64, k: int = 2) -> tuple[Path, Path]:
    """Par normal+height 16-bit coherente y BAND-LIMITED (una sola frecuencia por eje).

    El height de 16 bits es lo que hace alcanzable el NaN legítimo de ``hf_energy_ratio``:
    con 8 bits el ruido de cuantización sube la fracción HF por encima de 1e-6 y M7 no
    dispara. Este corpus EJERCITA el defecto con el camino real, no lo simula.
    """
    x = np.arange(n) / n
    xx, yy = np.meshgrid(x, x, indexing="xy")
    h = np.asarray(0.3 * np.sin(2 * np.pi * k * xx) + 0.2 * np.cos(2 * np.pi * k * yy), dtype=np.float64)
    h = (h - h.min()) / (h.max() - h.min())
    p, q = spectral_gradients(h)
    normal = normals_from_gradients(p, q)
    rgb = np.clip((normal * 0.5 + 0.5) * 255.0 + 0.5, 0, 255).astype(np.uint8)
    h16 = np.clip(h * 65535.0 + 0.5, 0, 65535).astype(np.uint16)
    adir = root / "originals" / asset_id
    adir.mkdir(parents=True, exist_ok=True)
    normal_path = adir / f"{asset_id}_nor.png"
    height_path = adir / f"{asset_id}_disp.png"
    Image.fromarray(rgb).save(normal_path)
    Image.fromarray(h16).save(height_path)
    return normal_path, height_path


def _synthetic_manifest(root: Path, n_assets: int = 3) -> Path:
    """Manifest M2 válido (CC0 + sha256 + convención) sobre assets sintéticos idénticos.

    Idénticos a propósito: toda candidata de σ_eff es constante sobre CALIBRATION, así
    que ningún ρ es evaluable ⇒ ``winner=None`` y ``status=NO_EVALUABLE_SIGMA_CANDIDATE``
    (MATH-B.1). El e2e ancla ESE estado —incluido ``"winner": null`` en el JSON— además
    del boundary estricto.
    """
    entries = []
    for i in range(n_assets):
        asset_id = f"syn_brick_{i:02d}"
        normal_path, height_path = _write_synthetic_asset(root, asset_id)
        entries.append(
            build_manifest_entry(
                asset_id=asset_id,
                family="brick",  # CALIBRATION_FAMILIES
                mirror="synthetic-repro-a",
                normal_path=normal_path,
                height_path=height_path,
                declared_convention="DIRECTX",  # forma que producen las normales sintéticas
                normal_variant="nor_dx",
            )
        )
    manifest = root / "manifest.json"
    manifest.write_text(json.dumps({"assets": entries}), encoding="utf-8")
    return manifest


def _run_main(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, resolution: int = 64) -> Path:
    root = tmp_path / "corpus"
    out = tmp_path / "out"
    manifest = _synthetic_manifest(root)
    monkeypatch.setattr(
        sys, "argv", ["run_exp_m2", "--manifest", str(manifest), "--out", str(out), "--resolution", str(resolution)]
    )
    m2.main()
    return out


def _assert_artefacto_estricto(path: Path) -> dict[str, Any]:
    texto = path.read_text(encoding="utf-8")

    assert "NaN" not in texto, f"{path.name}: literal NaN emitido"
    assert "Infinity" not in texto, f"{path.name}: literal Infinity emitido"
    assert _NAN_LITERAL.search(texto) is None
    assert _INF_LITERAL.search(texto) is None

    datos = json.loads(texto)
    # No alcanza con que Python se re-lea a sí mismo: con allow_nan=False un non-finite
    # olvidado falla explícitamente en vez de colarse como literal no estándar.
    json.dumps(datos, allow_nan=False)
    return datos


def test_main_escribe_artefactos_json_estrictos(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """§10/§11 e2e con fixtures sintéticos: ambos artefactos, sin literales no estándar.

    El corpus se ejecuta por el camino REAL (load_asset + characterize_asset + run_policy),
    sin tocar el corpus de 31 assets. Un no finito INTERNO (NaN en ``hf_energy_ratio``,
    ±inf en los ``r_*_proxy`` de la fila RAW) debe salir como ``null``.
    """
    out = _run_main(monkeypatch, tmp_path)

    rows = _assert_artefacto_estricto(out / "rows.json")
    characs = _assert_artefacto_estricto(out / "characs.json")

    filas = rows["rows"]
    assert len(filas) == 3
    # M4: la ruta NO-spearman también debe estar saneada.
    assert all(fila["hf_energy_ratio"] is None for fila in filas)
    assert all(fila["hf_gt_fraction"] < 1e-6 for fila in filas)  # el NaN era legítimo (M7)
    # ±inf de la fila RAW (σ_eff=0) → null, no "Infinity" ni 0.
    assert all(fila["r_p01_proxy"] is None for fila in filas)

    # characs.json (M2): el mismo contrato, en OTRO archivo y por otra ruta anidada.
    for charac in characs.values():
        assert charac["features"]["r_p01_proxy"] is None
        assert charac["raw_eval"]["hf_energy_ratio"] is None

    # §14: el estado científico MATH-B.1 no cambia de significado.
    assert rows["sigma_selection"]["winner"] is None
    assert rows["sigma_selection"]["status"] == "NO_EVALUABLE_SIGMA_CANDIDATE"
    assert '"winner": null' in (out / "rows.json").read_text(encoding="utf-8")
    assert all(tabla["spearman"] is None for tabla in rows["sigma_selection"]["table"])
    assert len(filas) == 3  # sin winner no hay M2-E/F: sólo las RAW ya calculadas


def test_main_es_fail_fast_si_el_sanitizado_falta(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """M3: con ``allow_nan`` por defecto el artefacto no estándar se EMITE en silencio.

    Se neutraliza sólo el sanitizado: el boundary debe seguir fallando, no degradar.
    """
    root = tmp_path / "corpus"
    out = tmp_path / "out"
    manifest = _synthetic_manifest(root)
    monkeypatch.setattr(
        sys, "argv", ["run_exp_m2", "--manifest", str(manifest), "--out", str(out), "--resolution", "64"]
    )
    monkeypatch.setattr(m2, "_json_safe", lambda obj: obj)

    with pytest.raises(ValueError, match="JSON compliant"):
        m2.main()


def test_main_no_altera_los_valores_internos_antes_del_boundary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """§15: lo que llega al boundary es el objeto INTERNO, intacto.

    El sanitizado se aplica a la copia serializable, no al estado que la corrida
    científica produjo; y todo lo finito sobrevive idéntico (mismo valor y tipo).
    """
    capturados: dict[str, Any] = {}
    real_write = m2._write_json

    def espia(path: Path, payload: Any) -> None:
        capturados[path.name] = payload  # el objeto INTERNO que llega al boundary
        real_write(path, payload)

    root = tmp_path / "corpus"
    out = tmp_path / "out"
    manifest = _synthetic_manifest(root)
    monkeypatch.setattr(
        sys, "argv", ["run_exp_m2", "--manifest", str(manifest), "--out", str(out), "--resolution", "64"]
    )
    monkeypatch.setattr(m2, "_write_json", espia)

    m2.main()

    payload = capturados["rows.json"]
    fila = payload["rows"][0]
    assert isinstance(fila["hf_energy_ratio"], float) and not np.isfinite(fila["hf_energy_ratio"])  # interno: NaN
    assert fila["gradient_rmse"] > 0.0  # finito intacto

    salida = (out / "rows.json").read_text(encoding="utf-8")
    leido = json.loads(salida)
    assert leido["rows"][0]["gradient_rmse"] == fila["gradient_rmse"]
    assert type(leido["rows"][0]["gradient_rmse"]) is type(fila["gradient_rmse"])
    assert leido["rows"][0]["hf_energy_ratio"] is None  # sólo la representación cambió

"""Quinta ronda correctiva #700 — Spearman no finito: JSON-safe y en la clave que se lee.

Finding del Regression & Test Oracle sobre el HEAD 3ba2609e (issue_header
"Incomplete UNAVAILABLE handling"), profundizado al ir al código: el guard previo no era
sólo asimétrico, era **inefectivo** en los dos extremos.

    if not np.isfinite(universes["BASELINE_COUNTERFACTUAL"]["grid"]["spearman_..."]) :
        universes["BASELINE_COUNTERFACTUAL"]["spearman_..."] = "UNAVAILABLE"

1. Escribía el centinela en `universes["BASELINE_COUNTERFACTUAL"]["spearman_..."]`, un
   nivel que NINGÚN consumidor consulta. El adjudicador lee
   `universes[<universo>][grid|continuous].spearman_deg_vs_delta_rmse`
   (`build_corrective_evidence._adjudicate_h1`), así que el `nan` quedaba publicado igual.
2. Sólo miraba `grid`. `continuous` y el universo `HISTORICAL_COMPARISON` quedaban sin
   guarda.
3. Y como la evidencia se escribe con `allow_nan=False`, un `nan` que llegara al archivo
   no se publicaba "en crudo": **tumbaba la corrida entera** con `ValueError`.

El contrato correcto: `spearman_or_unavailable` publica un `float` finito o el centinela
`"UNAVAILABLE"`, en la MISMA clave que leen los consumidores, para las cuatro ranuras.

En el corpus actual los cuatro valores son finitos, así que el cambio es inobservable en
la evidencia publicada (se fija abajo como propiedad medida, no asumida).

Tests RESEARCH-ONLY: sin red, sin corpus.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_CORR = (
    Path(__file__).resolve().parents[1]
    / "docs"
    / "validation"
    / "native-parallax-h1-h5-falsification-impact-audit-20261007"
    / "corrective-20261008"
)
_SCRIPTS = _CORR / "scripts"
_H1 = _CORR / "evidence" / "h1-corrected-evidence.json"

sys.path.insert(0, str(_SCRIPTS))


def _load(name: str):
    path = _SCRIPTS / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"corrective_{name}", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


pcc = _load("phase_c_corrective")
bce = _load("build_corrective_evidence")


def _h1_publicada() -> dict:
    return json.loads(_H1.read_text(encoding="utf-8"))


# ------------------------------------------------------- el centinela, en la primitiva
def test_spearman_finito_devuelve_float():
    v = pcc.spearman_or_unavailable([1.0, 2.0, 3.0, 4.0], [4.0, 3.0, 2.0, 1.0])
    assert isinstance(v, float)
    assert v == pytest.approx(-1.0)


def test_spearman_degenerado_devuelve_el_centinela():
    """Serie constante => `nan` (no finito) => centinela explícito, nunca `nan` publicado."""
    v = pcc.spearman_or_unavailable([1.0, 1.0, 1.0], [1.0, 2.0, 3.0])
    assert v == "UNAVAILABLE"


def test_spearman_con_entrada_no_finita_devuelve_el_centinela():
    """`spearman` levanta `ValueError` con NaN/Inf: se traduce al MISMO centinela."""
    v = pcc.spearman_or_unavailable([1.0, float("nan"), 3.0], [1.0, 2.0, 3.0])
    assert v == "UNAVAILABLE"


def test_el_formato_del_print_acepta_el_centinela():
    """El print de cierre no puede asumir `float` (`:.4f` sobre un str explota)."""
    assert pcc._fmt_spearman(0.4782258064516129) == "0.4782"
    assert pcc._fmt_spearman("UNAVAILABLE") == "UNAVAILABLE"


# ------------------------------------------- el centinela, donde lo lee el consumidor
def test_el_consumidor_lee_el_centinela_en_la_clave_donde_se_publica():
    """Prueba de contrato: el adjudicador ve el centinela si está en la clave correcta.

    Se inyecta el centinela en las cuatro ranuras sobre la evidencia REAL y se corre el
    adjudicador real. Si el valor viviera en una clave paralela —como antes— el
    adjudicador seguiría leyendo el número y este test fallaría.
    """
    h1 = _h1_publicada()
    for universo in ("BASELINE_COUNTERFACTUAL", "HISTORICAL_COMPARISON"):
        for sub in ("grid", "continuous"):
            h1["universes"][universo][sub]["spearman_deg_vs_delta_rmse"] = "UNAVAILABLE"

    adj = bce._adjudicate_h1(h1)
    assert adj["H1_SPEARMAN_BASELINE"] == "UNAVAILABLE"
    assert adj["H1_SPEARMAN_BASELINE_GRID"] == "UNAVAILABLE"
    assert adj["H1_SPEARMAN_HISTORICAL_HYBRID_CONTINUOUS"] == "UNAVAILABLE"


def test_la_adjudicacion_con_centinela_es_serializable():
    """`allow_nan=False` no puede tumbar el builder cuando el valor es el centinela."""
    h1 = _h1_publicada()
    h1["universes"]["BASELINE_COUNTERFACTUAL"]["grid"]["spearman_deg_vs_delta_rmse"] = "UNAVAILABLE"
    adj = bce._adjudicate_h1(h1)
    json.dumps(adj, allow_nan=False)


def test_un_nan_crudo_si_tumbaria_la_serializacion():
    """Contra-prueba: el motivo del centinela es real, no decorativo."""
    with pytest.raises(ValueError):
        json.dumps({"spearman": float("nan")}, allow_nan=False)


# ------------------------------------------------- el corpus actual: sin cambio alguno
def test_la_evidencia_publicada_tiene_los_cuatro_spearman_finitos():
    """Propiedad medida: en el corpus actual el guard es rama muerta => impacto NINGUNO."""
    u = _h1_publicada()["universes"]
    for universo in ("BASELINE_COUNTERFACTUAL", "HISTORICAL_COMPARISON"):
        for sub in ("grid", "continuous"):
            v = u[universo][sub]["spearman_deg_vs_delta_rmse"]
            assert isinstance(v, float), f"{universo}/{sub} no es float: {v!r}"
            assert v == v  # no NaN
            assert abs(v) != float("inf")


# ------------------------------------------------- ancla de fuente: enumerar, no muestrear
def test_las_cuatro_ranuras_usan_la_primitiva_segura():
    """Enumera por fuente: las 4 ranuras deben usar `spearman_or_unavailable`.

    Si alguien agrega un universo nuevo y usa el camino crudo, el conteo rompe. También
    se prohíbe el patrón viejo (centinela en una clave de nivel superior que nadie lee).
    """
    src = (_SCRIPTS / "phase_c_corrective.py").read_text(encoding="utf-8")
    assert src.count("spearman_or_unavailable(grid_deg,") == 2, (
        "grid debe usar la primitiva segura en los DOS universos"
    )
    assert src.count("spearman_or_unavailable(cont_deg,") == 2, (
        "continuous debe usar la primitiva segura en los DOS universos"
    )
    assert 'universes["BASELINE_COUNTERFACTUAL"]["spearman_deg_vs_delta_rmse"]' not in src, (
        "el centinela no puede volver a publicarse en una clave de nivel superior"
    )

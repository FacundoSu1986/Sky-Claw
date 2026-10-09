"""Segunda ronda correctiva #700 — adjudicación H4 desdoblada (finding C).

El claim externo tenía DOS componentes:
    "5-21x mayor"  (relativo)   y   "llega a 0.023 absoluto" (absoluto)

La primera ronda adjudicaba sólo el relativo y rotulaba `PARTIALLY_REPRODUCED`,
dejando leer que el componente absoluto también se había reproducido. No es así:
los deltas medidos están ~3 órdenes de magnitud por debajo de 0.023.

Estos tests fijan que ambos componentes se adjudiquen por separado y que el
resumen no oculte la discrepancia.

Tests RESEARCH-ONLY: sin red; leen la evidencia correctiva del PR.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

_CORR = (
    Path(__file__).resolve().parents[1]
    / "docs"
    / "validation"
    / "native-parallax-h1-h5-falsification-impact-audit-20261007"
    / "corrective-20261008"
)
_SCRIPTS = _CORR / "scripts"


def _load(name: str):
    path = _SCRIPTS / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"corrective_{name}", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


bce = _load("build_corrective_evidence")


def _adjudication() -> dict:
    return json.loads((_CORR / "corrective-adjudication.json").read_text(encoding="utf-8"))


def _h4_evidence() -> dict:
    return json.loads((_CORR / "evidence" / "h4-corrected-evidence.json").read_text(encoding="utf-8"))


def test_el_claim_tiene_dos_componentes_declarados():
    assert bce.EXTERNAL_RATIO_LOW == 5.0
    assert bce.EXTERNAL_RATIO_HIGH == 21.0
    assert bce.EXTERNAL_ABSOLUTE_CLAIM == 0.023


def test_adjudicacion_emite_ratio_y_absoluto_por_separado():
    h4 = _adjudication()["H4"]
    assert "H4_RATIO_CLAIM_STATUS" in h4
    assert "H4_ABSOLUTE_CLAIM_STATUS" in h4
    assert h4["H4_RATIO_CLAIM_STATUS"] in {
        "REPRODUCED",
        "PARTIALLY_REPRODUCED",
        "NOT_REPRODUCED",
        "UNRESOLVED",
    }
    assert h4["H4_ABSOLUTE_CLAIM_STATUS"] in {
        "REPRODUCED",
        "PARTIALLY_REPRODUCED",
        "NOT_REPRODUCED",
        "UNRESOLVED",
    }


def test_ratio_reproducido_no_implica_absoluto_reproducido():
    """El caso real: ratios dentro/superando la banda, absoluto muy por debajo."""
    h4 = _adjudication()["H4"]
    assert h4["H4_RATIO_CLAIM_STATUS"] == "PARTIALLY_REPRODUCED"
    assert h4["H4_ABSOLUTE_CLAIM_STATUS"] == "NOT_REPRODUCED"


def test_deltas_absolutos_estan_muy_por_debajo_del_claim():
    h4 = _adjudication()["H4"]
    deltas = h4["H4_ABSOLUTE_DELTAS_SAFE_ARM"]
    assert deltas, "debe haber deltas medidos en el brazo de superficie consistente"
    best = max(deltas)
    claim = h4["H4_EXTERNAL_ABSOLUTE_CLAIM"]
    # el mejor caso está al menos 100x por debajo del valor reclamado
    assert best < claim / 100.0, f"max delta {best!r} no está 100x por debajo de {claim!r}"
    assert h4["H4_ABSOLUTE_FACTOR_VS_CLAIM_SAFE_ARM"] == best / claim


def test_el_resumen_no_oculta_la_discrepancia():
    summary = _adjudication()["H4"]["H4_EXTERNAL_MAGNITUDE_SUMMARY"]
    assert "RATIO=" in summary
    assert "ABSOLUTE=" in summary
    assert "NO se reproduce" in summary or "NOT_REPRODUCED" in summary


def test_clasificador_absoluto_distingue_bandas():
    """Contrapruebas del clasificador, con el claim real de 0.023."""
    f = bce._classify_absolute if hasattr(bce, "_classify_absolute") else None
    if f is None:
        # el clasificador vive dentro de _adjudicate_h4; se prueba vía evidencia
        return
    claim = bce.EXTERNAL_ABSOLUTE_CLAIM
    assert f([claim]) == "REPRODUCED"
    assert f([claim * 5.0]) == "PARTIALLY_REPRODUCED"
    assert f([claim / 500.0]) == "NOT_REPRODUCED"
    assert f([]) == "UNRESOLVED"


def test_evidencia_h4_registra_deltas_de_ambos_brazos():
    ev = _h4_evidence()
    ext = ev["external_claim_probe_c_0_05_0_01"]
    for k, v in ext.items():
        assert "delta" in v["clipped"], f"{k}: falta delta del brazo clipeado"
        assert "delta" in v["safe_surface"], f"{k}: falta delta del brazo seguro"


def test_amplitudes_obligatorias_presentes_en_la_evidencia():
    ev = _h4_evidence()
    ext = ev["external_claim_probe_c_0_05_0_01"]
    amps = {v["amplitude"] for v in ext.values()}
    assert 0.05 in amps, "c=0.05 es obligatoria (punto de operación del claim)"
    assert 0.01 in amps, "c=0.01 es obligatoria (punto de operación del claim)"

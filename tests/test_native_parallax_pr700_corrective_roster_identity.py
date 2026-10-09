"""Segunda ronda correctiva #700 — identidad del roster (finding F).

El guard anterior verificaba sólo `len(prepared) == 31`. Un manifest distinto con 31
assets válidos lo pasaba y publicaba M4/M5 como contrafactual del corpus equivocado.

Estos tests fijan que el guard nuevo **rechace** un roster que no sea exactamente el
histórico, y que acepte el correcto.

Tests RESEARCH-ONLY: sin red; leen el artefacto autorizado dentro de #700.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

_SCRIPTS = (
    Path(__file__).resolve().parents[1]
    / "docs"
    / "validation"
    / "native-parallax-h1-h5-falsification-impact-audit-20261007"
    / "corrective-20261008"
    / "scripts"
)


def _load(name: str):
    path = _SCRIPTS / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"corrective_{name}", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


c2 = _load("phase_c2_h4_corpus_corrective")


def _historical_ids() -> list[str]:
    doc = json.loads(c2.HISTORICAL_AUDIT_ARTIFACT.read_text(encoding="utf-8"))
    return sorted(str(r["asset"]) for r in doc["H1_coherence_oracle_counterfactual"]["rows"])


def test_artefacto_historico_tiene_31_y_digest_congelado():
    ids = _historical_ids()
    assert len(ids) == c2.HISTORICAL_ROSTER_SIZE == 31
    assert c2._roster_digest(ids) == c2.HISTORICAL_ROSTER_SHA256


def test_acepta_el_roster_historico_exacto():
    prepared = [{"asset_id": a} for a in _historical_ids()]
    res = c2.check_roster_identity(prepared, c2.DEFAULT_MANIFEST)
    assert res["roster_count_match"] is True
    assert res["roster_identity_match"] is True
    assert res["roster_digest_match"] is True
    assert res["manifest_digest_match"] is True
    assert res["only_current"] == []
    assert res["only_historical"] == []


def test_rechaza_roster_del_mismo_tamano_pero_distinto():
    """El caso que el guard viejo dejaba pasar: 31 assets, pero no los históricos."""
    ids = _historical_ids()
    impostor = [*ids[:-1], "polyhaven_no_existe_asset"]  # 31 ids, uno distinto
    assert len(impostor) == 31
    prepared = [{"asset_id": a} for a in impostor]
    res = c2.check_roster_identity(prepared, c2.DEFAULT_MANIFEST)
    assert res["roster_count_match"] is True  # el conteo solo NO alcanza
    assert res["roster_identity_match"] is False
    assert res["roster_digest_match"] is False
    assert res["only_current"] == ["polyhaven_no_existe_asset"]
    assert res["only_historical"] == [ids[-1]]


def test_rechaza_roster_mas_chico():
    prepared = [{"asset_id": a} for a in _historical_ids()[:15]]
    res = c2.check_roster_identity(prepared, c2.DEFAULT_MANIFEST)
    assert res["roster_count_match"] is False
    assert res["roster_identity_match"] is False


def test_emite_el_digest_del_manifest():
    prepared = [{"asset_id": a} for a in _historical_ids()]
    res = c2.check_roster_identity(prepared, c2.DEFAULT_MANIFEST)
    assert len(res["manifest_sha256"]) == 64
    assert res["manifest_path"].endswith("exp-m3-clean-authored-manifest.json")


def test_evidencia_regenerada_trae_el_bloque_de_identidad():
    """La evidencia emitida debe incluir el bloque de identidad verificado."""
    ev = (
        Path(__file__).resolve().parents[1]
        / "docs"
        / "validation"
        / "native-parallax-h1-h5-falsification-impact-audit-20261007"
        / "corrective-20261008"
        / "evidence"
        / "h4-corpus-counterfactual.json"
    )
    doc = json.loads(ev.read_text(encoding="utf-8"))
    ri = doc.get("roster_identity")
    assert ri is not None, "la evidencia debe emitir roster_identity"
    assert ri["roster_count_match"] is True
    assert ri["roster_identity_match"] is True
    assert ri["roster_digest_match"] is True
    assert ri["manifest_digest_match"] is True

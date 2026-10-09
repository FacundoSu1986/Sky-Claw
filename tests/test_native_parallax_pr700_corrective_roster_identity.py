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
    # Renombrado (ronda 3): el nombre viejo `manifest_digest_match` describía mal lo que
    # calculaba (el digest del roster HISTÓRICO contra el SHA congelado, no el del archivo).
    assert res["historical_roster_digest_matches_frozen_sha"] is True
    # Contrato SEPARADO: el archivo del manifiesto M3 congelado (canónico LF).
    assert res["m3_manifest_sha256_matches_frozen"] is True
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
    assert len(res["manifest_sha256_lf"]) == 64
    assert len(res["manifest_sha256_worktree"]) == 64
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
    assert ri["historical_roster_digest_matches_frozen_sha"] is True
    assert ri["m3_manifest_sha256_matches_frozen"] is True
    # El gate debe haber permitido el cálculo y quedar registrado en la evidencia.
    assert doc.get("roster_gate", {}).get("allowed") is True
    assert doc["roster_gate"]["failed_conditions"] == []


# ---------------------------------------------------------------------------
# Ronda 3 — el digest congelado tiene que PARTICIPAR de la decisión, no sólo
# calcularse. Los tests de arriba verificaban valores con los archivos actuales;
# ninguno probaba que `main()`/el gate RECHAZARA un digest histórico incorrecto.
# ---------------------------------------------------------------------------


def test_gate_acepta_el_estado_actual_verificado():
    prepared = [{"asset_id": a} for a in _historical_ids()]
    res = c2.check_roster_identity(prepared, c2.DEFAULT_MANIFEST)
    gate = c2.roster_gate(res)
    assert gate["allowed"] is True
    assert gate["failed_conditions"] == []


def test_gate_rechaza_digest_historico_congelado_incongruente(tmp_path, monkeypatch):
    """Caso adversarial del finding nuevo.

    Se altera el artefacto histórico y se prepara el corpus con ESE MISMO roster
    alterado: count=True e identity=True, pero el digest histórico NO coincide con
    el SHA congelado. El gate debe bloquear igual (fail-closed)."""
    ids = _historical_ids()
    alterado = [*ids[:-1], "polyhaven_asset_inyectado"]
    doc = json.loads(c2.HISTORICAL_AUDIT_ARTIFACT.read_text(encoding="utf-8"))
    doc["H1_coherence_oracle_counterfactual"]["rows"] = [{"asset": a} for a in alterado]
    falso = tmp_path / "real-impact.json"
    falso.write_text(json.dumps(doc), encoding="utf-8")
    monkeypatch.setattr(c2, "HISTORICAL_AUDIT_ARTIFACT", falso)

    res = c2.check_roster_identity([{"asset_id": a} for a in alterado], c2.DEFAULT_MANIFEST)
    assert res["roster_count_match"] is True  # el conteo solo NO alcanza
    assert res["roster_identity_match"] is True  # coincide con el artefacto ALTERADO
    assert res["historical_roster_digest_matches_frozen_sha"] is False
    gate = c2.roster_gate(res)
    assert gate["allowed"] is False
    assert "historical_roster_digest_matches_frozen_sha" in gate["failed_conditions"]


def test_gate_rechaza_manifest_alterado_con_el_mismo_roster(tmp_path):
    """Manifest con los MISMOS asset-ids pero bytes distintos (p.ej. un path cambiado).

    La identidad de ids pasa; el digest del archivo no. El gate debe bloquear: si no,
    se mediría otro archivo de imagen conservando el mismo asset_id."""
    ids = _historical_ids()
    doc = json.loads(c2.DEFAULT_MANIFEST.read_text(encoding="utf-8"))
    doc["_tamper_path"] = "assets/otro_archivo.png"
    falso = tmp_path / "manifest.json"
    falso.write_text(json.dumps(doc, indent=1), encoding="utf-8")
    res = c2.check_roster_identity([{"asset_id": a} for a in ids], falso)
    assert res["roster_count_match"] is True
    assert res["roster_identity_match"] is True
    assert res["historical_roster_digest_matches_frozen_sha"] is True
    assert res["m3_manifest_sha256_matches_frozen"] is False
    gate = c2.roster_gate(res)
    assert gate["allowed"] is False
    assert "m3_manifest_sha256_matches_frozen" in gate["failed_conditions"]


def test_gate_es_fail_closed_si_falta_una_condicion():
    """Una clave ausente o no booleana bloquea: el gate no asume True por defecto."""
    gate = c2.roster_gate({"roster_count_match": True, "roster_identity_match": True})
    assert gate["allowed"] is False
    assert gate["failed_conditions"] == [
        "historical_roster_digest_matches_frozen_sha",
        "m3_manifest_sha256_matches_frozen",
    ]


def test_gate_es_fail_closed_ante_valores_no_booleanos():
    gate = c2.roster_gate(
        {
            "roster_count_match": 1,  # truthy pero no True
            "roster_identity_match": True,
            "historical_roster_digest_matches_frozen_sha": True,
            "m3_manifest_sha256_matches_frozen": True,
        }
    )
    assert gate["allowed"] is False
    assert gate["failed_conditions"] == ["roster_count_match"]


def test_manifest_digest_canonico_es_independiente_del_eol():
    """El digest congelado del manifiesto es el canónico LF: un checkout LF (Linux) y
    uno CRLF (Windows) producen el MISMO valor. Congelar el digest CRLF rompería el
    gate en Linux."""
    raw = c2.DEFAULT_MANIFEST.read_bytes()
    lf = raw.replace(b"\r\n", b"\n")
    crlf = lf.replace(b"\n", b"\r\n")
    assert c2._bytes_digest_lf(lf) == c2._bytes_digest_lf(crlf)
    assert c2._manifest_digest_lf(c2.DEFAULT_MANIFEST) == c2.EXPECTED_M3_MANIFEST_SHA256_LF


def test_manifest_digest_lf_no_es_el_digest_crlf_del_worktree():
    """Deja explícita la relación EOL que el protocolo M2/M3 ya documenta."""
    import hashlib

    raw = c2.DEFAULT_MANIFEST.read_bytes()
    assert hashlib.sha256(raw).hexdigest() != c2.EXPECTED_M3_MANIFEST_SHA256_LF
    assert c2._manifest_digest_lf(c2.DEFAULT_MANIFEST) != hashlib.sha256(raw).hexdigest()

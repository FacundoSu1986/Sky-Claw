"""Micro-slice final #700 — C2 obligatorio, fail-closed y con propagación M4.

Dos findings materiales fuera del diff inline:

FINDING_C2_REQUIRED_FAIL_CLOSED
    `build_corrective_evidence.py` aceptaba `--c2` opcional (`default=None`) y, si el
    archivo no existía, seguía adelante publicando `M4_PRIMARY_STATUS` /
    `M5_PRIMARY_STATUS` como `NOT_INVALIDATED` sin ninguna evidencia de identidad de
    roster. Eso contradice el contrato fail-closed de la propia auditoría.

FINDING_C2_M4_DECISION_PROPAGATION
    El C2 registra `m4.decision_changed`, pero el builder no lo leía: un C2 con
    `decision_changed=True` seguía publicando `M4_PRIMARY_STATUS=NOT_INVALIDATED`.

Estos tests fijan la matriz A–H del brief §13 más los casos de estructura y de CLI.
Tests RESEARCH-ONLY: sin red; usan la evidencia C2 real del PR como base.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import subprocess
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
_EV = _CORR / "evidence"
_H1 = _EV / "h1-corrected-evidence.json"
_H4 = _EV / "h4-corrected-evidence.json"
_DET = _EV / "determinism.json"
_C2_REAL = _EV / "h4-corpus-counterfactual.json"
_SCRIPT = _SCRIPTS / "build_corrective_evidence.py"


def _load(name: str):
    path = _SCRIPTS / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"corrective_{name}", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


bce = _load("build_corrective_evidence")


def _real_c2() -> dict:
    return json.loads(_C2_REAL.read_text(encoding="utf-8"))


def _run_builder(tmp_path: Path, c2_arg: Path | None, *, omit_c2: bool = False):
    """Corre el builder real con la evidencia real y el C2 indicado."""
    out = tmp_path / "corrective-adjudication.json"
    cmd = [
        sys.executable,
        str(_SCRIPT),
        "--h1",
        str(_H1),
        "--h4",
        str(_H4),
        "--determinism",
        str(_DET),
        "--out",
        str(out),
    ]
    if not omit_c2:
        cmd += ["--c2", str(c2_arg)]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    return proc, out


def _write_c2(tmp_path: Path, doc: dict) -> Path:
    p = tmp_path / "c2.json"
    p.write_text(json.dumps(doc), encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Matriz A–H (§13)
# ---------------------------------------------------------------------------


def test_a_c2_valido_m4_no_cambiada(tmp_path):
    """A. C2 válido + m4.decision_changed=False -> M4 y M5 NOT_INVALIDATED."""
    doc = _real_c2()
    doc["m4"]["decision_changed"] = False
    proc, out = _run_builder(tmp_path, _write_c2(tmp_path, doc))
    assert proc.returncode == 0, proc.stderr
    adj = json.loads(out.read_text(encoding="utf-8"))
    assert adj["M4_PRIMARY_STATUS"] == "NOT_INVALIDATED"
    assert adj["M5_PRIMARY_STATUS"] == "NOT_INVALIDATED"


def test_b_c2_valido_m4_cambiada_propaga_a_unresolved(tmp_path):
    """B. C2 válido + m4.decision_changed=True -> M4 UNRESOLVED, M5 NOT_INVALIDATED."""
    doc = _real_c2()
    doc["m4"]["decision_changed"] = True
    proc, out = _run_builder(tmp_path, _write_c2(tmp_path, doc))
    assert proc.returncode == 0, proc.stderr
    adj = json.loads(out.read_text(encoding="utf-8"))
    assert adj["M4_PRIMARY_STATUS"] == "UNRESOLVED"
    assert adj["M5_PRIMARY_STATUS"] == "NOT_INVALIDATED"
    assert adj["C2_M4_DECISION_CHANGED"] is True


def test_c_c2_omitido_hard_stop(tmp_path):
    """C. --c2 omitido -> no se genera adjudicación con M4/M5 finales."""
    proc, out = _run_builder(tmp_path, None, omit_c2=True)
    assert proc.returncode != 0, "el builder debe fallar sin --c2"
    assert not out.exists(), "no debe publicarse adjudicación parcial"


def test_c2_apunta_a_archivo_inexistente_hard_stop(tmp_path):
    """--c2 dado pero el archivo no existe -> hard stop, no adjudicación parcial."""
    proc, out = _run_builder(tmp_path, tmp_path / "no-existe.json")
    assert proc.returncode != 0
    assert not out.exists()


def test_d_roster_gate_allowed_false_hard_stop(tmp_path):
    """D. roster_gate.allowed=False -> hard stop."""
    doc = _real_c2()
    doc["roster_gate"]["allowed"] = False
    proc, out = _run_builder(tmp_path, _write_c2(tmp_path, doc))
    assert proc.returncode != 0
    assert not out.exists()


def test_e_condicion_requerida_false_hard_stop(tmp_path):
    """E. una condición requerida en False -> hard stop."""
    doc = _real_c2()
    doc["roster_identity"]["roster_identity_match"] = False
    proc, out = _run_builder(tmp_path, _write_c2(tmp_path, doc))
    assert proc.returncode != 0
    assert not out.exists()


def test_f_condicion_requerida_ausente_hard_stop(tmp_path):
    """F. una condición requerida ausente -> hard stop (no se asume True)."""
    doc = _real_c2()
    del doc["roster_identity"]["m3_manifest_sha256_matches_frozen"]
    proc, out = _run_builder(tmp_path, _write_c2(tmp_path, doc))
    assert proc.returncode != 0
    assert not out.exists()


def test_g_condicion_requerida_truthy_no_bool_hard_stop(tmp_path):
    """G. una condición requerida = 1 (truthy pero no True) -> hard stop."""
    doc = _real_c2()
    doc["roster_identity"]["roster_count_match"] = 1
    proc, out = _run_builder(tmp_path, _write_c2(tmp_path, doc))
    assert proc.returncode != 0
    assert not out.exists()


def test_h_h4_decisional_preserva_unresolved():
    """H. H4 decisional -> M4 y M5 UNRESOLVED (el gate existente NO se debilita)."""
    res = bce.resolve_primary_statuses("DECISIONAL", "NUMERICAL_NOT_DECISIONAL", c2_m4_decision_changed=False)
    assert res["M4_PRIMARY_STATUS"] == "UNRESOLVED"
    assert res["M5_PRIMARY_STATUS"] == "UNRESOLVED"
    assert res["H4_DECISIONAL"] is True
    # también si sólo M5 es decisional
    res5 = bce.resolve_primary_statuses("NUMERICAL_NOT_DECISIONAL", "DECISIONAL", c2_m4_decision_changed=False)
    assert res5["M4_PRIMARY_STATUS"] == "UNRESOLVED"
    assert res5["M5_PRIMARY_STATUS"] == "UNRESOLVED"


# ---------------------------------------------------------------------------
# Estructura del C2 (fail-closed) — función pura
# ---------------------------------------------------------------------------


def test_validador_acepta_la_evidencia_real():
    res = bce.validate_c2_for_adjudication(_real_c2())
    assert res["valid"] is True
    assert res["failed_conditions"] == []
    assert res["m4_decision_changed"] is False


def test_validador_rechaza_c2_no_objeto():
    assert bce.validate_c2_for_adjudication([])["valid"] is False
    assert bce.validate_c2_for_adjudication(None)["valid"] is False


def test_validador_rechaza_roster_identity_no_dict():
    doc = _real_c2()
    doc["roster_identity"] = "no-es-un-dict"
    res = bce.validate_c2_for_adjudication(doc)
    assert res["valid"] is False
    assert "roster_identity_is_object" in res["failed_conditions"]


def test_validador_rechaza_roster_gate_no_dict():
    doc = _real_c2()
    doc["roster_gate"] = None
    res = bce.validate_c2_for_adjudication(doc)
    assert res["valid"] is False
    assert "roster_gate_is_object" in res["failed_conditions"]


def test_validador_exige_m4_decision_changed_booleano():
    """Sin `m4.decision_changed` legible no se puede descartar un cambio: fail-closed."""
    doc = _real_c2()
    del doc["m4"]["decision_changed"]
    res = bce.validate_c2_for_adjudication(doc)
    assert res["valid"] is False
    assert "m4.decision_changed_is_bool" in res["failed_conditions"]


def test_validador_enumera_las_cuatro_condiciones_exactas():
    assert bce.C2_REQUIRED_ROSTER_CONDITIONS == (
        "roster_count_match",
        "roster_identity_match",
        "historical_roster_digest_matches_frozen_sha",
        "m3_manifest_sha256_matches_frozen",
    )


def test_validador_no_acepta_truthy_no_bool_en_cada_condicion():
    for cond in bce.C2_REQUIRED_ROSTER_CONDITIONS:
        for bad in (1, "true", [], {}, None):
            doc = copy.deepcopy(_real_c2())
            doc["roster_identity"][cond] = bad
            res = bce.validate_c2_for_adjudication(doc)
            assert res["valid"] is False, f"{cond}={bad!r} debió bloquear"
            assert cond in res["failed_conditions"]


# ---------------------------------------------------------------------------
# Evidencia real del PR y adjudicación publicada
# ---------------------------------------------------------------------------


def test_evidencia_c2_real_pasa_el_gate_nuevo():
    """§14/§15: la evidencia real debe pasar; si no, BLOCKER, no se acomoda el gate."""
    doc = _real_c2()
    assert doc["roster_gate"]["allowed"] is True
    assert doc["m4"]["decision_changed"] is False
    assert bce.validate_c2_for_adjudication(doc)["valid"] is True


def test_adjudicacion_publicada_mantiene_m4_m5_not_invalidated():
    adj = json.loads((_CORR / "corrective-adjudication.json").read_text(encoding="utf-8"))
    assert adj["M4_PRIMARY_STATUS"] == "NOT_INVALIDATED"
    assert adj["M5_PRIMARY_STATUS"] == "NOT_INVALIDATED"
    assert adj["C2_VALIDATED_FOR_ADJUDICATION"] is True
    assert adj["C2_M4_DECISION_CHANGED"] is False
    assert adj["SCIENTIFIC_RESULT_CHANGED"] == "NO"


def test_cli_exige_c2_en_el_parser():
    """El parser debe declarar --c2 como obligatorio."""
    src = _SCRIPT.read_text(encoding="utf-8")
    assert "required=True" in src
    # el patrón viejo (opcional) no debe sobrevivir
    assert 'ap.add_argument("--c2", type=Path, default=None' not in src

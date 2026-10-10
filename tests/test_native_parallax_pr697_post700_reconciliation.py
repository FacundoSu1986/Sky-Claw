"""Invariantes documentales de la reconciliación post-#700 sobre el PR #697.

Congela una clase de defecto, no un caso: una superficie nueva del directorio
de #697 no puede publicar `PR675_RECOMMENDATION=READY_FOR_DESIGN_RECONCILIATION`
como vigente al mismo tiempo que `KEEP_DRAFT_BLOCKED`. La familia es el roster
de archivos del directorio de auditoría; un archivo nuevo sin clasificar rompe
el ancla.

Tests RESEARCH-ONLY: sin red; sólo leen documentos del PR y hashean blobs.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_AUDIT = _REPO / "docs" / "validation" / "native-parallax-post-reval1-m4-m5-impact-audit-20261007"
_PR700_ADJ = (
    _REPO
    / "docs"
    / "validation"
    / "native-parallax-h1-h5-falsification-impact-audit-20261007"
    / "corrective-20261008"
    / "corrective-adjudication.json"
)

# Roster cerrado. Agregar un archivo al directorio exige clasificarlo acá:
# ¿es evidencia histórica congelada, registro de reconciliación, o una
# superficie nueva de PR675_RECOMMENDATION?
_ROSTER_CERRADO = {
    "README.md",
    "impact-matrix.json",
    "provenance.json",
    "callgraph-evidence.json",
    "post-700-reconciliation.json",
}

_BLOBS_HISTORICOS = {
    "impact-matrix.json": "570617da347b515b4065a0c0dade9640ca24a7ee",
    "provenance.json": "cbdb89edb80244ad39fd090397894840ca3d54a2",
    "callgraph-evidence.json": "5460f368e63ac90c88794330a3f6be76c1272381",
}

_ASSIGNMENT = re.compile(
    r"(?P<key>PR675_RECOMMENDATION(?:_HISTORICAL|_STATUS|_SOURCE|_CONSISTENT)?"
    r"|M6_IMPLEMENTATION_BLOCKED"
    r"|PR697_DISPOSITION"
    r"|M4_PRIMARY_STATUS"
    r"|M5_PRIMARY_STATUS"
    r"|M4_IMPACT_ADJUDICATION"
    r"|M5_IMPACT_ADJUDICATION)"
    r"""\s*[=:]\s*["']?(?P<value>[A-Z][A-Z0-9_]*)"""
)

_HISTORICAL_MARKERS = (
    "SUPERSEDED",
    "SUPERADA",
    "histórica",
    "historica",
    "HISTORICAL",
    "PR675_RECOMMENDATION_HISTORICAL",
)


def _blob_sha1(path: Path) -> str:
    data = path.read_bytes()
    return hashlib.sha1(b"blob " + str(len(data)).encode("ascii") + b"\0" + data).hexdigest()


def _recon() -> dict:
    return json.loads((_AUDIT / "post-700-reconciliation.json").read_text(encoding="utf-8"))


def _matrix() -> dict:
    return json.loads((_AUDIT / "impact-matrix.json").read_text(encoding="utf-8"))


def _pr700() -> dict:
    return json.loads(_PR700_ADJ.read_text(encoding="utf-8"))


def test_el_roster_del_directorio_de_auditoria_esta_cerrado() -> None:
    """Un archivo nuevo en el directorio de #697 obliga a clasificarlo.

    Si mañana alguien agrega un markdown o JSON con otra recomendación vigente,
    este test se pone rojo hasta que el roster y las reglas de autoridad se
    actualicen de forma explícita.
    """
    presentes = {p.name for p in _AUDIT.iterdir() if p.is_file()}
    assert presentes == _ROSTER_CERRADO, (
        f"roster del directorio de #697 cambió: {sorted(presentes)} vs "
        f"{sorted(_ROSTER_CERRADO)}. Clasificá el archivo nuevo: evidencia "
        "histórica congelada, registro de reconciliación, o superficie de "
        "PR675_RECOMMENDATION."
    )
    extra_dirs = [p.name for p in _AUDIT.iterdir() if p.is_dir()]
    assert extra_dirs == [], f"subdirectorios no previstos: {extra_dirs}"


def test_los_tres_json_historicos_conservan_el_blob_de_eb066a1() -> None:
    """La reconciliación no reescribe la evidencia numérica de #697."""
    for nombre, esperado in _BLOBS_HISTORICOS.items():
        actual = _blob_sha1(_AUDIT / nombre)
        assert actual == esperado, (
            f"{nombre} cambió de blob: {actual} vs {esperado} (eb066a1). "
            "La evidencia histórica no se reescribe para parecer contemporánea."
        )
    recon = _recon()
    for nombre, esperado in _BLOBS_HISTORICOS.items():
        assert recon["historical_blob_sha1"][nombre] == esperado


def test_m4_y_m5_siguen_not_invalidated_y_no_se_relabelan() -> None:
    """NOT_INVALIDATED no se convierte en REVALIDATED en ninguna superficie."""
    matrix = _matrix()
    adj = matrix["adjudications"]
    assert adj["M4_IMPACT_ADJUDICATION"] == "NOT_INVALIDATED"
    assert adj["M5_IMPACT_ADJUDICATION"] == "NOT_INVALIDATED"
    assert adj["M4_RERUN_REQUIRED"] == "NO"
    assert adj["M5_RERUN_REQUIRED"] == "NO"

    recon = _recon()
    auth = recon["authority"]
    assert auth["M4_PRIMARY_STATUS"] == "NOT_INVALIDATED"
    assert auth["M5_PRIMARY_STATUS"] == "NOT_INVALIDATED"
    assert auth["M4_IMPACT_ADJUDICATION"] == "NOT_INVALIDATED"
    assert auth["M5_IMPACT_ADJUDICATION"] == "NOT_INVALIDATED"
    assert auth["NOT_INVALIDATED_IS_NOT_REVALIDATED"] is True
    assert auth["PR697_DISPOSITION"] == "STILL_VALID_NARROW_SCOPE"

    pr700 = _pr700()
    assert pr700["M4_PRIMARY_STATUS"] == "NOT_INVALIDATED"
    assert pr700["M5_PRIMARY_STATUS"] == "NOT_INVALIDATED"
    assert pr700["SCIENTIFIC_RESULT_CHANGED"] == "NO"
    assert pr700["PR697_DISPOSITION"] == "STILL_VALID_NARROW_SCOPE"

    for path in _AUDIT.iterdir():
        if not path.is_file():
            continue
        texto = path.read_text(encoding="utf-8")
        assert "M4_PRIMARY_STATUS=REVALIDATED" not in texto
        assert "M5_PRIMARY_STATUS=REVALIDATED" not in texto
        assert "M4_IMPACT_ADJUDICATION=REVALIDATED" not in texto
        assert "M5_IMPACT_ADJUDICATION=REVALIDATED" not in texto


def _ocurrencias_pr675(texto: str) -> list[tuple[int, str, str, str]]:
    """Devuelve (linea, key, value, ventana) para cada asignación PR675_*."""
    lineas = texto.splitlines()
    out: list[tuple[int, str, str, str]] = []
    for i, linea in enumerate(lineas):
        for match in _ASSIGNMENT.finditer(linea):
            key = match.group("key")
            if not key.startswith("PR675_RECOMMENDATION"):
                continue
            lo = max(0, i - 4)
            hi = min(len(lineas), i + 5)
            ventana = "\n".join(lineas[lo:hi])
            out.append((i + 1, key, match.group("value"), ventana))
    return out


def test_ninguna_superficie_publica_ready_y_keep_draft_como_vigentes() -> None:
    """La familia de asignaciones vigentes de PR675_RECOMMENDATION es unitaria.

    READY_FOR_DESIGN_RECONCILIATION puede aparecer como histórica SUPERSEDED.
    KEEP_DRAFT_BLOCKED es la única recomendación vigente. Publicar READY
    sin marcador histórico es el defecto que esta reconciliación cierra.
    """
    vigentes: list[tuple[str, int, str]] = []
    historicas: list[tuple[str, int, str]] = []
    ready_sin_marcador: list[tuple[str, int]] = []
    for path in sorted(_AUDIT.iterdir()):
        if not path.is_file():
            continue
        for linea, key, value, ventana in _ocurrencias_pr675(path.read_text(encoding="utf-8")):
            if key != "PR675_RECOMMENDATION":
                continue
            if value == "KEEP_DRAFT_BLOCKED":
                vigentes.append((path.name, linea, value))
            elif value == "READY_FOR_DESIGN_RECONCILIATION":
                if any(m in ventana for m in _HISTORICAL_MARKERS):
                    historicas.append((path.name, linea, value))
                else:
                    ready_sin_marcador.append((path.name, linea))
            else:
                raise AssertionError(
                    f"{path.name}:{linea} publica PR675_RECOMMENDATION={value} fuera del contrato histórico/vigente"
                )

    assert not ready_sin_marcador, (
        f"READY_FOR_DESIGN_RECONCILIATION vigente (sin SUPERSEDED/HISTORICAL): {ready_sin_marcador}"
    )
    assert vigentes, "no hay PR675_RECOMMENDATION vigente; el contrato quedó mudo"
    valores_vigentes = {v for _, _, v in vigentes}
    assert valores_vigentes == {"KEEP_DRAFT_BLOCKED"}, vigentes
    assert historicas, "la recomendación histórica desapareció; no se reescribe el registro"
    valores_hist = {v for _, _, v in historicas}
    assert valores_hist == {"READY_FOR_DESIGN_RECONCILIATION"}, historicas


def test_el_json_de_reconciliacion_es_la_autoridad_machine_readable() -> None:
    recon = _recon()
    pr675 = recon["pr675"]
    assert pr675["PR675_RECOMMENDATION"] == "KEEP_DRAFT_BLOCKED"
    assert pr675["PR675_RECOMMENDATION_STATUS"] == "CURRENT"
    assert pr675["PR675_RECOMMENDATION_HISTORICAL"] == "READY_FOR_DESIGN_RECONCILIATION"
    assert pr675["PR675_RECOMMENDATION_HISTORICAL_STATUS"] == "SUPERSEDED"
    assert pr675["M6_IMPLEMENTATION_BLOCKED"] == "YES"
    assert pr675["READY_FOR_M6_IMPLEMENTATION"] == "NO"
    assert pr675["READY_FOR_DESIGN_RECONCILIATION"] == "NO"
    assert pr675["PR675_CHANGED_BY_THIS_SLICE"] is False

    contracts = recon["contracts"]
    assert contracts == {
        "PR697_NARROW_SCOPE_PRESERVED": "YES",
        "M4_DECISION_NOT_SILENTLY_RELABELED": "YES",
        "M5_DECISION_NOT_SILENTLY_RELABELED": "YES",
        "PR675_RECOMMENDATION_CONSISTENT": "YES",
        "M6_IMPLEMENTATION_BLOCKED": "YES",
        "HISTORICAL_EVIDENCE_UNCHANGED": "YES",
        "JSON_VALID": "YES",
        "MAIN_CONTAINS_PR700": "YES",
    }

    decisions = recon["independent_decisions"]
    assert decisions["A_pr697_narrow_conclusion_still_valid"]["verdict"] == "YES"
    assert decisions["B_open_675_documentary_reconciliation"]["verdict"] == "NO"
    assert decisions["C_implement_m6"]["verdict"] == "NO"

    assert recon["h4_resize_normal"]["H4_RESIZE_NORMAL_DESIGN_DEPENDENCY"] == "UNRESOLVED"
    assert recon["m5_legacy_heldout"]["M5_LEGACY_HELDOUT_LIMITATION"] == ("DECLARED_NOT_IN_C2_COUNTERFACTUAL")

    pr700 = _pr700()
    assert pr700["PR675_RECOMMENDATION"] == recon["pr675"]["PR675_RECOMMENDATION"]
    assert pr700["M6_IMPLEMENTATION_BLOCKED"] == "YES"
    assert pr700["H1"]["H1_CONVERGED_ASSETS"] == 31
    assert pr700["H1"]["H1_UNRESOLVED_ASSETS"] == 0
    assert recon["authority"]["H1_CONVERGED_ASSETS"] == 31
    assert recon["authority"]["H1_UNRESOLVED_ASSETS"] == 0


def test_h4_y_c2_citados_coinciden_con_los_artefactos_de_700() -> None:
    """No reconstruir números desde prosa si el artefacto está en el árbol."""
    pr700 = _pr700()
    recon = _recon()
    assert pr700["H4"]["H4_IMPLEMENTATION_DEFECT"] == "CONFIRMED"
    assert pr700["H4"]["H4_M4_PRIMARY_IMPACT"] == "NUMERICAL_NOT_DECISIONAL"
    assert pr700["H4"]["H4_M5_PRIMARY_IMPACT"] == "NUMERICAL_NOT_DECISIONAL"
    assert recon["authority"]["H4_M4_PRIMARY_IMPACT"] == "NUMERICAL_NOT_DECISIONAL"
    assert recon["h4_resize_normal"]["c2_corpus"]["m4_decision_changed"] is False
    assert pr700["C2_M4_DECISION_CHANGED"] is False

    c2_path = _REPO / recon["h4_resize_normal"]["c2_corpus"]["path"]
    c2 = json.loads(c2_path.read_text(encoding="utf-8"))
    assert c2["m4"]["decision_changed"] is False
    assert c2["m4"]["decision_old"] == c2["m4"]["decision_new"]
    assert "LEGACY_HELDOUT" in c2["m5"]["note"]


def test_readme_declara_keep_draft_blocked_como_vigente() -> None:
    texto = (_AUDIT / "README.md").read_text(encoding="utf-8")
    assert "PR675_RECOMMENDATION                   = KEEP_DRAFT_BLOCKED" in texto
    assert "PR675_RECOMMENDATION_HISTORICAL_STATUS = SUPERSEDED" in texto
    assert "M6_IMPLEMENTATION_BLOCKED              = YES" in texto
    assert "READY_FOR_M6_IMPLEMENTATION            = NO" in texto
    assert "## 12. Reconciliación posterior a PR #700" in texto

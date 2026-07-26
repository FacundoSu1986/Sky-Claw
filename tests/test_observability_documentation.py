from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parent.parent


def test_documentacion_declara_perdida_acotada_sin_fallback_sincrono() -> None:
    deployment = (ROOT / "DEPLOYMENT.md").read_text(encoding="utf-8")
    observability = (ROOT / "docs/operations/observability.md").read_text(encoding="utf-8")
    combined = deployment + observability
    assert "8192" in combined
    assert "WARNING" in combined
    assert "256" in combined
    assert "pueden perderse" in combined
    assert "fallback síncrono" in combined


def test_documentacion_release_distingue_cosign_de_authenticode() -> None:
    deployment = (ROOT / "DEPLOYMENT.md").read_text(encoding="utf-8")
    release = (ROOT / "docs/operations/release.md").read_text(encoding="utf-8")
    combined = deployment + release
    assert "v0.2.4" in combined
    assert "Cosign" in combined
    assert "SBOM" in combined
    assert "Authenticode" in combined
    assert "SkyClawApp.exe.bundle.json" in combined

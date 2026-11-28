"""Invariantes de atribuciones de terceros, integridad documental y exportación CLI (PR-LIC-01)."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST_PATH = REPO_ROOT / "licenses" / "third_party" / "manifest.json"
NOTICES_PATH = REPO_ROOT / "THIRD_PARTY_NOTICES.md"
FONTS_OFL_PATH = REPO_ROOT / "sky_claw" / "app" / "gui" / "assets" / "fonts" / "OFL.txt"
SPEC_PATH = REPO_ROOT / "sky_claw.spec"

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _sha256_archivo(ruta: Path) -> str:
    return hashlib.sha256(ruta.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def _parsear_paquetes_lock(ruta_lock: Path) -> dict[str, str]:
    paquetes: dict[str, str] = {}
    for linea in ruta_lock.read_text(encoding="utf-8").splitlines():
        limpia = linea.strip()
        if not limpia or limpia.startswith("#"):
            continue
        m = re.match(r"^([A-Za-z0-9_.-]+)==([^ ;\\\s]+)", limpia)
        if m:
            nombre = m.group(1).lower().replace("_", "-")
            paquetes[nombre] = m.group(2)
    return paquetes


def _cargar_manifiesto() -> dict[str, Any]:
    assert MANIFEST_PATH.is_file(), f"Falta el manifiesto de licencias: {MANIFEST_PATH}"
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


# ── 1. Integridad del manifiesto y textos verbatim ───────────────────────────


def test_manifiesto_terceros_tiene_esquema_y_hashes_validos() -> None:
    """Cada archivo listado en manifest.json existe, no está vacío y coincide byte a byte en SHA-256."""
    manifiesto = _cargar_manifiesto()
    assert manifiesto["schema_version"] == 1
    documentos = manifiesto["documents"]
    assert isinstance(documentos, list) and len(documentos) > 0

    rutas_vistas: set[str] = set()
    for doc in documentos:
        rel = doc["path"]
        assert rel not in rutas_vistas, f"Ruta duplicada en manifest.json: {rel}"
        rutas_vistas.add(rel)
        assert not rel.startswith("/") and ".." not in Path(rel).parts, f"Ruta insegura: {rel}"
        assert _SHA256_RE.fullmatch(doc["sha256"]), f"SHA-256 malformado para {rel}: {doc['sha256']!r}"
        ruta_real = REPO_ROOT / rel
        assert ruta_real.is_file(), f"Documento faltante en el repo: {rel}"
        datos = ruta_real.read_bytes().replace(b"\r\n", b"\n")
        assert len(datos) > 0, f"Documento vacío: {rel}"
        assert len(datos) == doc["bytes"], f"Tamaño distinto para {rel}"
        assert hashlib.sha256(datos).hexdigest() == doc["sha256"], f"Hash alterado para {rel}"

    # No debe haber archivos huérfanos bajo licenses/third_party/texts/
    textos_en_disco = {
        p.relative_to(REPO_ROOT).as_posix()
        for p in (REPO_ROOT / "licenses" / "third_party" / "texts").rglob("*")
        if p.is_file()
    }
    textos_en_manifiesto = {r for r in rutas_vistas if r.startswith("licenses/third_party/texts/")}
    assert textos_en_disco == textos_en_manifiesto, (
        f"Desajuste entre disco y manifiesto: extra={textos_en_disco - textos_en_manifiesto}, "
        f"faltantes={textos_en_manifiesto - textos_en_disco}"
    )


def test_cobertura_efectiva_locks_y_licencias_compuestas() -> None:
    """Todo paquete de los locks debe estar clasificado (redistribuido o excluido verificado) con su versión exacta."""
    manifiesto = _cargar_manifiesto()
    lock_np = _parsear_paquetes_lock(REPO_ROOT / "requirements-native-parallax.lock")
    lock_base = _parsear_paquetes_lock(REPO_ROOT / "requirements.lock")
    assert set(lock_base).issubset(set(lock_np))

    componentes = {c["name"]: c for c in manifiesto["components"] if c["type"] == "pypi_package"}
    excluidos = set(manifiesto["verified_not_redistributed_pypi"])

    # Ningún paquete del lock puede quedar sin clasificar (detecta paquete nuevo sin revisión)
    assert set(lock_np) == set(componentes) | excluidos, (
        f"Paquetes sin clasificar en manifest.json: {set(lock_np) - (set(componentes) | excluidos)}"
    )
    assert not (set(componentes) & excluidos), "Un paquete no puede ser redistribuido y excluido a la vez"

    rutas_docs = {d["path"] for d in manifiesto["documents"]}
    for nombre, comp in componentes.items():
        assert comp["version"] == lock_np[nombre], f"Versión desactualizada para {nombre}"
        assert comp["license_documents"], f"{nombre} no referencia ningún documento de licencia"
        for doc_rel in comp["license_documents"]:
            assert doc_rel in rutas_docs, f"{nombre} apunta a documento inexistente: {doc_rel}"

    # Ancla de licencias compuestas y obligaciones reconciliadas (L01-04 / L01-05)
    assert "Apache-2.0" in componentes["aiohttp"]["license_concluded"]
    assert "MIT" in componentes["aiohttp"]["license_concluded"]
    assert "MPL-2.0" in componentes["orjson"]["license_concluded"]
    assert "MPL-2.0" in componentes["grpcio"]["license_concluded"]
    assert "BSD-2-Clause" in componentes["prometheus-client"]["license_concluded"]
    assert "BSD-3-Clause" in componentes["backports-zstd"]["license_concluded"]
    assert componentes["cffi"]["license_concluded"] == "MIT-0"
    assert componentes["h11"]["license_concluded"] == "MIT"
    assert componentes["rarfile"]["license_concluded"] == "ISC"
    for mpl_pkg in ("bidict", "certifi", "grpcio", "orjson"):
        assert componentes[mpl_pkg]["source_obligation"] == "YES_MPL_2_0_SEC_3_2"
    for lgpl_pkg in ("py7zr", "inflate64", "multivolumefile", "pybcj", "pyppmd"):
        assert componentes[lgpl_pkg]["license_concluded"] == "LGPL-2.1-or-later"
        assert componentes[lgpl_pkg]["source_obligation"] == "YES_LGPL_2_1_SEC_6"


# ── 2. Preservación de Hermes, OFL-1.1 por familia y sky_claw.spec ───────────


def test_third_party_notices_preserva_hermes_ofl_y_obligaciones_pendientes() -> None:
    """THIRD_PARTY_NOTICES.md conserva Hermes Agent, 4 familias OFL, nativos, NiceGUI y reservas PR-LIC-02/04."""
    texto = NOTICES_PATH.read_text(encoding="utf-8")
    for requerido in (
        "Hermes Agent",
        "Nous Research",
        "Cinzel",
        "Cinzel Decorative",
        "EB Garamond",
        "Noto Sans Runic",
        "Spline Sans Mono",
        "PyInstaller",
        "Bootloader Exception",
        "OpenSSL",
        "DOMPurify",
        "PR-LIC-02",
        "PR-LIC-04",
    ):
        assert requerido in texto, f"Falta referencia obligatoria en THIRD_PARTY_NOTICES.md: {requerido}"

    # No afirmar falsamente que todas las obligaciones LGPL/MPL quedan cumplidas por el NOTICE
    assert "no constituye cumplimiento integral" in texto.lower() or "pr-lic-02" in texto.lower()


def test_fuentes_ofl_txt_presente_con_cuatro_familias() -> None:
    """sky_claw/app/gui/assets/fonts/OFL.txt existe con texto OFL-1.1 íntegro y las 4 familias."""
    assert FONTS_OFL_PATH.is_file(), "Falta sky_claw/app/gui/assets/fonts/OFL.txt"
    contenido = FONTS_OFL_PATH.read_text(encoding="utf-8")
    assert "SIL OPEN FONT LICENSE Version 1.1" in contenido
    for copyright_esperado in (
        "Copyright 2020 The Cinzel Project Authors",
        "Copyright 2017 The EB Garamond Project Authors",
        "Copyright 2022 The Noto Project Authors",
        "Copyright 2022 The Spline Sans Mono Project Authors",
    ):
        assert copyright_esperado in contenido


def test_sky_claw_spec_empaqueta_avisos_y_licencias() -> None:
    """sky_claw.spec incluye THIRD_PARTY_NOTICES.md y licenses/third_party en datas."""
    spec = SPEC_PATH.read_text(encoding="utf-8")
    assert '("THIRD_PARTY_NOTICES.md", "sky_claw/legal")' in spec
    assert '("licenses/third_party", "sky_claw/legal/licenses/third_party")' in spec


# ── 3. Exportación CLI --export-licenses (L01-07) ────────────────────────────


def test_export_licenses_exporta_paquete_integro_incluso_sin_stdout_ni_stderr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--export-licenses crea el directorio destino, exporta todos los documentos y verifica sus hashes con stdout/stderr=None."""
    from sky_claw.__main__ import exportar_licencias_terceros

    with tempfile.TemporaryDirectory(prefix="skyclaw_lic_export_") as base_externa:
        destino = Path(base_externa) / "licencias_exportadas"
        monkeypatch.setattr(sys, "stdout", None)
        monkeypatch.setattr(sys, "stderr", None)

        codigo = exportar_licencias_terceros(destino, origen_base=REPO_ROOT)

        assert codigo == 0
        assert (destino / "THIRD_PARTY_NOTICES.md").is_file()
        assert (destino / "licenses" / "third_party" / "manifest.json").is_file()
        manifiesto = _cargar_manifiesto()
        for doc in manifiesto["documents"]:
            exportado = destino / doc["path"]
            assert exportado.is_file(), f"Falta documento exportado: {doc['path']}"
            assert _sha256_archivo(exportado) == doc["sha256"]


def test_export_licenses_falla_cerrado_si_destino_ya_existe(tmp_path: Path) -> None:
    """No sobrescribe ni borra un directorio existente."""
    from sky_claw.__main__ import exportar_licencias_terceros

    destino = tmp_path / "existente"
    destino.mkdir()
    marcador = destino / "archivo_usuario.txt"
    marcador.write_text("no tocar", encoding="utf-8")

    codigo = exportar_licencias_terceros(destino, origen_base=REPO_ROOT)

    assert codigo != 0
    assert marcador.read_text(encoding="utf-8") == "no tocar"


def test_export_licenses_falla_cerrado_si_archivo_fuente_esta_alterado_y_limpia(tmp_path: Path) -> None:
    """Un documento alterado respecto al manifiesto provoca fallo cerrado y limpia solo el destino creado."""
    from sky_claw.__main__ import exportar_licencias_terceros

    origen_falso = tmp_path / "origen_alterado"
    (origen_falso / "licenses" / "third_party" / "texts").mkdir(parents=True)
    doc_falso = origen_falso / "licenses" / "third_party" / "texts" / "demo.txt"
    doc_falso.write_bytes(b"contenido alterado")
    (origen_falso / "THIRD_PARTY_NOTICES.md").write_text("notices", encoding="utf-8")
    manifiesto_falso = {
        "schema_version": 1,
        "documents": [
            {
                "path": "licenses/third_party/texts/demo.txt",
                "bytes": len(b"contenido original"),
                "sha256": hashlib.sha256(b"contenido original").hexdigest(),
            }
        ],
    }
    (origen_falso / "licenses" / "third_party" / "manifest.json").write_text(
        json.dumps(manifiesto_falso), encoding="utf-8"
    )
    destino = tmp_path / "salida_fallida"

    codigo = exportar_licencias_terceros(destino, origen_base=origen_falso)

    assert codigo != 0
    assert not destino.exists()


def test_export_licenses_falla_cerrado_si_manifiesto_es_invalido(tmp_path: Path) -> None:
    """Manifiesto JSON corrupto falla cerrado sin dejar directorio destino."""
    from sky_claw.__main__ import exportar_licencias_terceros

    origen_falso = tmp_path / "origen_roto"
    (origen_falso / "licenses" / "third_party").mkdir(parents=True)
    (origen_falso / "licenses" / "third_party" / "manifest.json").write_text("{corrupto", encoding="utf-8")
    destino = tmp_path / "salida_rota"

    codigo = exportar_licencias_terceros(destino, origen_base=origen_falso)

    assert codigo != 0
    assert not destino.exists()


def test_export_licenses_rechaza_destino_dentro_del_origen(tmp_path: Path) -> None:
    """Rechaza exportar dentro del propio directorio origen (ej. sys._MEIPASS)."""
    from sky_claw.__main__ import exportar_licencias_terceros

    destino_interno = REPO_ROOT / "licenses" / "third_party" / "subdir_prohibido"
    codigo = exportar_licencias_terceros(destino_interno, origen_base=REPO_ROOT)

    assert codigo != 0
    assert not destino_interno.exists()


def test_export_licenses_rechaza_symlink_en_destino(tmp_path: Path) -> None:
    """Rechaza un enlace simbólico como destino o padre."""
    from sky_claw.__main__ import exportar_licencias_terceros

    objetivo = tmp_path / "objetivo"
    objetivo.mkdir()
    enlace = tmp_path / "enlace"
    try:
        os.symlink(objetivo, enlace, target_is_directory=True)
    except OSError:
        pytest.skip("El entorno Windows actual no permite crear symlinks sin privilegios")

    codigo = exportar_licencias_terceros(enlace / "salida", origen_base=REPO_ROOT)
    assert codigo != 0
    assert not (objetivo / "salida").exists()

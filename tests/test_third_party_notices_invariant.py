"""Invariantes de atribuciones de terceros, integridad documental y exportación CLI (PR-LIC-01 R1)."""

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

#: Expresiones compuestas exactas (AND/OR) verificadas por bytes primarios (L01-04 / L01-11).
_EXPRESIONES_COMPUESTAS_EXACTAS: dict[str, str] = {
    "aiohttp": "Apache-2.0 AND MIT",
    "orjson": "MPL-2.0 AND (Apache-2.0 OR MIT)",
    "grpcio": "Apache-2.0 AND BSD-3-Clause AND MPL-2.0",
    "prometheus-client": "Apache-2.0 AND BSD-2-Clause",
    "backports-zstd": "PSF-2.0 AND BSD-3-Clause",
    "pycryptodomex": "Public-Domain AND BSD-2-Clause",
    "docutils": "Public-Domain AND BSD-2-Clause AND BSD-0-Clause AND PSF-2.0",
    "numpy": "BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 AND Apache-2.0",
}

#: Las 18 entradas frontend vendorizadas de NiceGUI que deben estar registradas individualmente (L01-09).
_FRONTEND_VENDORIZADO_ESPERADO: frozenset[str] = frozenset(
    {
        "js:quasar",
        "js:vue",
        "js:socket.io-client",
        "js:immutable",
        "js:dompurify",
        "js:leaflet",
        "js:threejs",
        "js:sortablejs",
        "js:xterm",
        "js:tailwindcss",
        "js:sass",
        "js:unocss",
        "js:mermaid",
        "js:codemirror",
        "js:echarts",
        "js:plotly",
        "js:ag-grid-community",
        "font:nicegui-roboto-material",
    }
)


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


def validar_manifiesto_contra_inventario_efectivo(
    manifiesto: dict[str, Any],
    paquetes_efectivamente_redistribuidos: set[str],
) -> list[str]:
    """Valida que ningún paquete efectivamente redistribuido figure como excluido ni falte en el manifiesto."""
    errores: list[str] = []
    registrados = {c["name"] for c in manifiesto["components"] if c["type"] == "pypi_package"}
    excluidos = set(manifiesto["verified_not_redistributed_pypi"])
    mal_excluidos = paquetes_efectivamente_redistribuidos & excluidos
    if mal_excluidos:
        errores.append(f"Paquetes efectivamente redistribuidos marcados como excluidos: {sorted(mal_excluidos)}")
    faltantes = paquetes_efectivamente_redistribuidos - registrados
    if faltantes:
        errores.append(f"Paquetes efectivamente redistribuidos ausentes del manifiesto: {sorted(faltantes)}")
    return errores


# ── 1. Integridad del manifiesto, textos verbatim y cobertura vendorizada ────


def test_manifiesto_terceros_tiene_esquema_y_hashes_validos() -> None:
    """Cada archivo listado en manifest.json existe, es ruta autorizada y coincide byte a byte en SHA-256."""
    from sky_claw.__main__ import _validar_ruta_relativa_manifiesto

    manifiesto = _cargar_manifiesto()
    assert manifiesto["schema_version"] == 1
    documentos = manifiesto["documents"]
    assert isinstance(documentos, list) and len(documentos) > 0

    rutas_vistas_norm: set[str] = set()
    rutas_exactas: set[str] = set()
    for doc in documentos:
        rel = doc["path"]
        assert _validar_ruta_relativa_manifiesto(rel) is not None, f"Ruta rechazada: {rel!r}"
        clave_norm = rel.casefold()
        assert clave_norm not in rutas_vistas_norm, f"Colisión case-insensitive en manifest.json: {rel}"
        rutas_vistas_norm.add(clave_norm)
        rutas_exactas.add(rel)
        assert _SHA256_RE.fullmatch(doc["sha256"]), f"SHA-256 malformado para {rel}: {doc['sha256']!r}"
        ruta_real = REPO_ROOT / rel
        assert ruta_real.is_file(), f"Documento faltante en el repo: {rel}"
        datos = ruta_real.read_bytes().replace(b"\r\n", b"\n")
        assert len(datos) > 0, f"Documento vacío: {rel}"
        assert len(datos) == doc["bytes"], f"Tamaño distinto para {rel}"
        assert hashlib.sha256(datos).hexdigest() == doc["sha256"], f"Hash alterado para {rel}"

    # Documentos obligatorios presentes
    assert "THIRD_PARTY_NOTICES.md" in rutas_exactas
    assert "sky_claw/app/gui/assets/fonts/OFL.txt" in rutas_exactas

    # No debe haber archivos huérfanos bajo licenses/third_party/texts/
    textos_en_disco = {
        p.relative_to(REPO_ROOT).as_posix()
        for p in (REPO_ROOT / "licenses" / "third_party" / "texts").rglob("*")
        if p.is_file()
    }
    textos_en_manifiesto = {r for r in rutas_exactas if r.startswith("licenses/third_party/texts/")}
    assert textos_en_disco == textos_en_manifiesto, (
        f"Desajuste entre disco y manifiesto: extra={textos_en_disco - textos_en_manifiesto}, "
        f"faltantes={textos_en_manifiesto - textos_en_disco}"
    )


def test_cobertura_efectiva_locks_expresiones_exactas_y_vendorizados() -> None:
    """Valida cobertura contra locks, expresiones AND/OR exactas, CPython Doc/license.rst y 18 frontend vendorizados."""
    manifiesto = _cargar_manifiesto()
    lock_np = _parsear_paquetes_lock(REPO_ROOT / "requirements-native-parallax.lock")
    lock_base = _parsear_paquetes_lock(REPO_ROOT / "requirements.lock")
    assert set(lock_base).issubset(set(lock_np))

    componentes = {c["name"]: c for c in manifiesto["components"] if c["type"] == "pypi_package"}
    excluidos = set(manifiesto["verified_not_redistributed_pypi"])

    assert set(lock_np) == set(componentes) | excluidos
    assert not (set(componentes) & excluidos)

    rutas_docs = {d["path"] for d in manifiesto["documents"]}
    for nombre, comp in componentes.items():
        assert comp["version"] == lock_np[nombre], f"Versión desactualizada para {nombre}"
        assert comp["license_documents"], f"{nombre} no referencia ningún documento de licencia"
        for doc_rel in comp["license_documents"]:
            assert doc_rel in rutas_docs, f"{nombre} apunta a documento inexistente: {doc_rel}"

    # Expresiones compuestas AND/OR exactas (mutar AND -> OR debe fallar)
    for nombre, expr_exacta in _EXPRESIONES_COMPUESTAS_EXACTAS.items():
        assert componentes[nombre]["license_concluded"] == expr_exacta, (
            f"Expresión compuesta alterada para {nombre}: {componentes[nombre]['license_concluded']!r} != {expr_exacta!r}"
        )

    assert componentes["cffi"]["license_concluded"] == "MIT-0"
    assert componentes["h11"]["license_concluded"] == "MIT"
    assert componentes["rarfile"]["license_concluded"] == "ISC"
    for mpl_pkg in ("bidict", "certifi", "grpcio", "orjson"):
        assert componentes[mpl_pkg]["source_obligation"] == "YES_MPL_2_0_SEC_3_2"
    for lgpl_pkg in ("py7zr", "inflate64", "multivolumefile", "pybcj", "pyppmd"):
        assert componentes[lgpl_pkg]["license_concluded"] == "LGPL-2.1-or-later"
        assert componentes[lgpl_pkg]["source_obligation"] == "YES_LGPL_2_1_SEC_6"

    # L01-09: CPython incluye tanto LICENSE como Doc/license.rst
    nativos = {c["component_id"]: c for c in manifiesto["components"] if c["type"] == "native_component"}
    assert len(nativos["native:cpython-runtime"]["license_documents"]) >= 2

    # L01-09 / L01-13: 18 bibliotecas y fuentes frontend vendorizadas de NiceGUI registradas individualmente
    vendorizados = {c["component_id"]: c for c in manifiesto["components"] if c["type"] == "vendored_frontend_asset"}
    assert set(vendorizados) == _FRONTEND_VENDORIZADO_ESPERADO
    for vid, vcomp in vendorizados.items():
        assert vcomp["document_coverage"] in ("FULL_TEXT_VERIFIED", "PENDING_DOCUMENTARY_EVIDENCE")
        assert vcomp["license_documents"], f"Biblioteca frontend sin documento legal: {vid}"
        for doc_rel in vcomp["license_documents"]:
            assert doc_rel in rutas_docs, f"{vid} apunta a documento inexistente: {doc_rel}"


def test_frontend_full_text_verified_incluye_texto_integro_no_solo_banner() -> None:
    """L01-13 (M9): cada componente frontend FULL_TEXT_VERIFIED debe referenciar texto legal íntegro, no solo un banner."""
    manifiesto = _cargar_manifiesto()
    marcas_texto_integro = (
        "Permission is hereby granted",
        "Redistribution and use in source and binary forms",
        "Apache License",
    )
    verificados = [
        c
        for c in manifiesto["components"]
        if c["type"] == "vendored_frontend_asset" and c.get("document_coverage") == "FULL_TEXT_VERIFIED"
    ]
    assert len(verificados) >= 9
    for comp in verificados:
        tiene_texto_integro = False
        for doc_rel in comp["license_documents"]:
            if "nicegui_vendored_frontend_notices" in doc_rel:
                continue  # El índice de banners no cuenta como texto legal íntegro
            texto = (REPO_ROOT / doc_rel).read_text(encoding="utf-8", errors="replace")
            if len(texto) >= 500 and any(m in texto for m in marcas_texto_integro):
                tiene_texto_integro = True
                break
        assert tiene_texto_integro, (
            f"Componente frontend FULL_TEXT_VERIFIED sin texto legal íntegro (solo banner): {comp['component_id']}"
        )


def test_reconciliador_falla_si_paquete_efectivo_se_reclasifica_como_excluido() -> None:
    """L01-11 (#3): reclasificar un componente efectivamente redistribuido como excluido debe fallar."""
    manifiesto = _cargar_manifiesto()
    manifiesto_mutado = json.loads(json.dumps(manifiesto))
    manifiesto_mutado["verified_not_redistributed_pypi"].append("aiohttp")
    manifiesto_mutado["components"] = [c for c in manifiesto_mutado["components"] if c["name"] != "aiohttp"]

    errores = validar_manifiesto_contra_inventario_efectivo(manifiesto_mutado, {"aiohttp", "nicegui"})
    assert len(errores) == 2


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
    """sky_claw.spec incluye THIRD_PARTY_NOTICES.md y licenses/third_party en datas bajo sky_claw/legal."""
    spec = SPEC_PATH.read_text(encoding="utf-8")
    assert '("THIRD_PARTY_NOTICES.md", "sky_claw/legal")' in spec
    assert '("licenses/third_party", "sky_claw/legal/licenses/third_party")' in spec


# ── 3. Exportación CLI --export-licenses y negativos L01-10 / L01-11 ─────────


def _crear_origen_minimo(base: Path, docs_extra: list[dict[str, Any]] | None = None) -> Path:
    """Crea un origen sintético completo y válido para probar mutaciones de exportación."""
    (base / "licenses" / "third_party" / "texts").mkdir(parents=True, exist_ok=True)
    (base / "sky_claw" / "app" / "gui" / "assets" / "fonts").mkdir(parents=True, exist_ok=True)
    pares = [
        ("THIRD_PARTY_NOTICES.md", b"# Notices\n"),
        ("sky_claw/app/gui/assets/fonts/OFL.txt", b"SIL OPEN FONT LICENSE Version 1.1\n"),
        ("licenses/third_party/texts/demo.txt", b"MIT License\n"),
    ]
    docs: list[dict[str, Any]] = []
    for rel, raw in pares:
        p = base / rel
        p.write_bytes(raw)
        docs.append({"path": rel, "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()})
    if docs_extra:
        docs.extend(docs_extra)
    componentes = [
        {
            "component_id": "pypi:demo@1.0.0",
            "name": "demo",
            "version": "1.0.0",
            "type": "pypi_package",
            "document_coverage": "FULL_TEXT_VERIFIED",
            "license_documents": ["licenses/third_party/texts/demo.txt"],
        }
    ]
    (base / "licenses" / "third_party" / "manifest.json").write_text(
        json.dumps({"schema_version": 1, "documents": docs, "components": componentes}), encoding="utf-8"
    )
    return base


def test_export_licenses_falla_si_falta_documento_referenciado_por_componente_lgpl(tmp_path: Path) -> None:
    """L01-12 (M8): eliminar un texto legal referenciado por un componente LGPL falla sin crear destino."""
    from sky_claw.__main__ import exportar_licencias_terceros

    origen = _crear_origen_minimo(tmp_path / "origen_lgpl")
    mp = origen / "licenses" / "third_party" / "manifest.json"
    m = json.loads(mp.read_text(encoding="utf-8"))
    m["components"].append(
        {
            "component_id": "pypi:py7zr@1.1.3",
            "name": "py7zr",
            "version": "1.1.3",
            "type": "pypi_package",
            "document_coverage": "FULL_TEXT_VERIFIED",
            "license_documents": ["licenses/third_party/texts/lgpl_faltante.txt"],
        }
    )
    mp.write_text(json.dumps(m), encoding="utf-8")
    destino = tmp_path / "salida_lgpl_faltante"

    codigo = exportar_licencias_terceros(destino, origen_base=origen)

    assert codigo != 0
    assert not destino.exists()


def test_export_licenses_falla_si_hay_component_id_duplicado(tmp_path: Path) -> None:
    """L01-12: component_id duplicado en manifest.json falla cerrado sin crear destino."""
    from sky_claw.__main__ import exportar_licencias_terceros

    origen = _crear_origen_minimo(tmp_path / "origen_dup")
    mp = origen / "licenses" / "third_party" / "manifest.json"
    m = json.loads(mp.read_text(encoding="utf-8"))
    m["components"].append(dict(m["components"][0]))
    mp.write_text(json.dumps(m), encoding="utf-8")
    destino = tmp_path / "salida_dup"

    codigo = exportar_licencias_terceros(destino, origen_base=origen)

    assert codigo != 0
    assert not destino.exists()


def test_export_licenses_exporta_paquete_integro_incluso_sin_stdout_ni_stderr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--export-licenses exporta todos los documentos y verifica sus hashes con stdout/stderr=None."""
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


@pytest.mark.parametrize(
    "ruta_peligrosa",
    [
        "/etc/passwd",
        "C:\\Windows\\System32\\evil.txt",
        "C:evil.txt",
        "\\\\server\\share\\evil.txt",
        "\\\\?\\C:\\evil.txt",
        "licenses\\third_party\\texts\\..\\evil.txt",
        "licenses/third_party/texts/../evil.txt",
        "licenses/third_party/texts/demo.txt:ads",
        "licenses/third_party/texts/CON.txt",
        "licenses/third_party/texts/nul",
        "licenses/third_party/texts/sub/anidado.txt",
        "sky_claw/config.py",
    ],
)
def test_export_licenses_rechaza_rutas_peligrosas_windows_y_fuera_de_perimetro(
    tmp_path: Path, ruta_peligrosa: str
) -> None:
    """L01-10 / L01-11 (#5): rechaza rutas absolutas, backslashes, traversal, ADS, reservadas y fuera de perímetro."""
    from sky_claw.__main__ import exportar_licencias_terceros

    origen = _crear_origen_minimo(
        tmp_path / "origen",
        docs_extra=[{"path": ruta_peligrosa, "bytes": 4, "sha256": hashlib.sha256(b"abcd").hexdigest()}],
    )
    destino = tmp_path / "salida_rechazada"

    codigo = exportar_licencias_terceros(destino, origen_base=origen)

    assert codigo != 0
    assert not destino.exists()


def test_export_licenses_rechaza_colision_case_insensitive_windows(tmp_path: Path) -> None:
    """L01-10 / L01-11 (#6): dos rutas que colisionan bajo semántica Windows (Demo.txt vs demo.txt) fallan cerrado."""
    from sky_claw.__main__ import exportar_licencias_terceros

    origen = _crear_origen_minimo(tmp_path / "origen")
    raw = b"MIT License\n"
    # Añadir entrada con diferente casing en manifest.json
    mp = origen / "licenses" / "third_party" / "manifest.json"
    m = json.loads(mp.read_text(encoding="utf-8"))
    m["documents"].append(
        {
            "path": "licenses/third_party/texts/DEMO.txt",
            "bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
        }
    )
    mp.write_text(json.dumps(m), encoding="utf-8")
    destino = tmp_path / "salida_colision"

    codigo = exportar_licencias_terceros(destino, origen_base=origen)

    assert codigo != 0
    assert not destino.exists()


def test_export_licenses_falla_si_falta_documento_obligatorio_en_manifiesto(tmp_path: Path) -> None:
    """L01-10 / L01-11 (#4): eliminar un documento obligatorio (OFL.txt) del manifiesto falla cerrado."""
    from sky_claw.__main__ import exportar_licencias_terceros

    origen = _crear_origen_minimo(tmp_path / "origen")
    mp = origen / "licenses" / "third_party" / "manifest.json"
    m = json.loads(mp.read_text(encoding="utf-8"))
    m["documents"] = [d for d in m["documents"] if d["path"] != "sky_claw/app/gui/assets/fonts/OFL.txt"]
    mp.write_text(json.dumps(m), encoding="utf-8")
    destino = tmp_path / "salida_incompleta"

    codigo = exportar_licencias_terceros(destino, origen_base=origen)

    assert codigo != 0
    assert not destino.exists()


def test_rollback_elimina_solo_archivos_creados_y_preserva_archivo_ajeno(tmp_path: Path) -> None:
    """L01-10 / L01-11 (#7): el rollback nunca borra un archivo ajeno presente en el árbol."""
    from sky_claw.__main__ import _rollback_creados

    raiz = tmp_path / "export_parcial"
    subdir = raiz / "licenses"
    subdir.mkdir(parents=True)
    creado = subdir / "creado.txt"
    creado.write_text("mio", encoding="utf-8")
    ajeno = subdir / "archivo_tercero.txt"
    ajeno.write_text("intocable", encoding="utf-8")

    _rollback_creados([creado], [raiz, subdir])

    assert not creado.exists()
    assert ajeno.is_file()
    assert ajeno.read_text(encoding="utf-8") == "intocable"


def test_export_licenses_falla_cerrado_si_archivo_fuente_esta_alterado_y_limpia(tmp_path: Path) -> None:
    """Un documento alterado respecto al manifiesto provoca fallo cerrado sin crear destino."""
    from sky_claw.__main__ import exportar_licencias_terceros

    origen = _crear_origen_minimo(tmp_path / "origen")
    (origen / "licenses" / "third_party" / "texts" / "demo.txt").write_bytes(b"alterado")
    destino = tmp_path / "salida_fallida"

    codigo = exportar_licencias_terceros(destino, origen_base=origen)

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


def test_export_licenses_rechaza_destino_dentro_del_origen() -> None:
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

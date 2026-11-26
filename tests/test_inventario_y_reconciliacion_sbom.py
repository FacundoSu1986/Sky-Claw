"""Pruebas TDD (AAA, negativas y de mutación) para inventario efectivo de EXE y reconciliación SBOM (PR-LIC-03A).

Cubre SBOM-01 a SBOM-08 sin ejecutar binarios reales ni importar módulos extraídos.
"""

from __future__ import annotations

import hashlib
import io
import json
import marshal
import struct
import zipfile
import zlib
from pathlib import Path
from typing import Any

import pytest

from scripts import inventario_ejecutable as inv
from scripts import reconciliar_sbom as rec

# ---------------------------------------------------------------------------
# Helpers sintéticos para construir un CArchive + PYZ + base_library.zip válidos
# ---------------------------------------------------------------------------

_COOKIE_MAGIC = b"MEI\014\013\012\013\016"
_COOKIE_FMT = "!8sIIII64s"
_TOC_FMT = "!IIIIBc"
_TOC_LEN = struct.calcsize(_TOC_FMT)


def _construir_pyz_bytes(modulos: dict[str, tuple[int, bytes]]) -> bytes:
    """Construye un contenedor PYZ compatible con PyInstaller ZlibArchiveReader."""
    buf = io.BytesIO()
    # Cabecera PYZ: magic (4B) + python magic (4B) + toc_offset (4B) + padding (4B) = 16 bytes
    buf.write(b"PYZ\x00")
    buf.write(b"\xa7\x0d\x0d\x0a")  # magic py3.11 sintético (check_pymagic=False)
    toc_pos_placeholder = buf.tell()
    buf.write(struct.pack("!I", 0))
    buf.write(b"\x00\x00\x00\x00")

    toc_list: list[tuple[str, tuple[int, int, int]]] = []
    for nombre, (tipo_pkg, contenido) in modulos.items():
        comprimido = zlib.compress(contenido)
        pos = buf.tell()
        buf.write(comprimido)
        toc_list.append((nombre, (tipo_pkg, pos, len(comprimido))))

    toc_offset = buf.tell()
    buf.write(marshal.dumps(toc_list))
    buf.seek(toc_pos_placeholder)
    buf.write(struct.pack("!I", toc_offset))
    return buf.getvalue()


def _construir_zip_bytes(miembros: list[tuple[str, bytes]]) -> bytes:
    """Construye un ZIP en memoria preservando incluso nombres duplicados si se pasan."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for nombre, datos in miembros:
            zf.writestr(nombre, datos)
    return buf.getvalue()


def _construir_carchive_exe(
    destino: Path,
    entradas: list[tuple[str, bytes, str, bool]],
    *,
    bootloader: bytes = b"MZ" + b"\x00" * 126,
    forzar_uncompressed_len: dict[str, int] | None = None,
    forzar_offset: dict[str, int] | None = None,
) -> Path:
    """Construye un EXE sintético con bootloader + CArchive PyInstaller válido.

    Cada tupla en ``entradas`` es ``(nombre, datos_crudos, typecode, comprimir)``.
    """
    pkg = io.BytesIO()
    toc_entries: list[tuple[str, int, int, int, int, bytes]] = []

    for nombre, datos, typecode, comprimir in entradas:
        offset = pkg.tell()
        if forzar_offset and nombre in forzar_offset:
            offset = forzar_offset[nombre]
        payload = zlib.compress(datos) if comprimir else datos
        pkg.write(payload)
        uncomp_len = len(datos)
        if forzar_uncompressed_len and nombre in forzar_uncompressed_len:
            uncomp_len = forzar_uncompressed_len[nombre]
        toc_entries.append((nombre, offset, len(payload), uncomp_len, 1 if comprimir else 0, typecode.encode("ascii")))

    toc_offset = pkg.tell()
    toc_buf = io.BytesIO()
    for nombre, offset, length, uncomp_len, comp_flag, tc in toc_entries:
        name_bytes = nombre.encode("utf-8") + b"\x00"
        padded_len = ((_TOC_LEN + len(name_bytes) + 15) // 16) * 16
        pad = padded_len - _TOC_LEN - len(name_bytes)
        toc_buf.write(struct.pack(_TOC_FMT, padded_len, offset, length, uncomp_len, comp_flag, tc))
        toc_buf.write(name_bytes + b"\x00" * pad)

    toc_bytes = toc_buf.getvalue()
    pkg.write(toc_bytes)
    pkg_body = pkg.getvalue()
    cookie_len = struct.calcsize(_COOKIE_FMT)
    archive_len = len(pkg_body) + cookie_len
    cookie = struct.pack(
        _COOKIE_FMT,
        _COOKIE_MAGIC,
        archive_len,
        toc_offset,
        len(toc_bytes),
        311,
        b"python311.dll".ljust(64, b"\x00"),
    )
    destino.write_bytes(bootloader + pkg_body + cookie)
    return destino


def _entorno_limpio() -> list[rec.PaqueteEntorno]:
    """Entorno de build sintético limpio con paquetes redistribuidos, instalados sin uso y solo-build."""
    return [
        rec.PaqueteEntorno(
            nombre="pydantic",
            version="2.12.5",
            licencia_declarada="MIT",
            licencia_concluida="MIT",
            evidencia="METADATA dist-info",
            confianza="ALTA",
            procedencia="PyPI",
            estado_revision_juridica="VERIFICADO",
            archivos_record=("pydantic/__init__.py", "pydantic/main.py"),
            modulos_top_level=("pydantic",),
            solo_build=False,
        ),
        rec.PaqueteEntorno(
            nombre="orjson",
            version="3.11.9",
            licencia_declarada="MPL-2.0 AND (Apache-2.0 OR MIT)",
            licencia_concluida="MPL-2.0 AND (Apache-2.0 OR MIT)",
            evidencia="METADATA dist-info",
            confianza="ALTA",
            procedencia="PyPI",
            estado_revision_juridica="VERIFICADO",
            archivos_record=("orjson/__init__.py", "orjson/orjson.cp311-win_amd64.pyd"),
            modulos_top_level=("orjson",),
            solo_build=False,
        ),
        rec.PaqueteEntorno(
            nombre="pytest",
            version="8.3.5",
            licencia_declarada="MIT",
            licencia_concluida="MIT",
            evidencia="METADATA dist-info",
            confianza="ALTA",
            procedencia="PyPI",
            estado_revision_juridica="VERIFICADO",
            archivos_record=("pytest/__init__.py",),
            modulos_top_level=("pytest",),
            solo_build=True,
        ),
        rec.PaqueteEntorno(
            nombre="paquete-no-empaquetado",
            version="1.0.0",
            licencia_declarada="BSD-3-Clause",
            licencia_concluida="BSD-3-Clause",
            evidencia="METADATA dist-info",
            confianza="ALTA",
            procedencia="PyPI",
            estado_revision_juridica="VERIFICADO",
            archivos_record=("no_empaquetado/__init__.py",),
            modulos_top_level=("no_empaquetado",),
            solo_build=False,
        ),
    ]


def _exe_completo_limpio(
    tmp_path: Path,
    *,
    incluir_pendientes_sbom07: bool = False,
    incluir_mo2_bridge: bool = True,
    incluir_pe_version_cpython: bool = True,
) -> Path:
    """Construye un EXE sintético con CArchive + PYZ + base_library.zip + datas de sky_claw.spec."""
    pyz_modulos: dict[str, tuple[int, bytes]] = {
        "sky_claw": (1, b"pyc-skyclaw"),
        "pydantic": (1, b"pyc-pydantic"),
        "pydantic.main": (0, b"pyc-pydantic-main"),
        "orjson": (1, b"pyc-orjson"),
    }
    if incluir_pendientes_sbom07:
        pyz_modulos["sky_claw.local.assets.auto_parallax_regla"] = (0, b"pyc-ap")

    pyz_bytes = _construir_pyz_bytes(pyz_modulos)
    base_lib_bytes = _construir_zip_bytes(
        [
            ("encodings/__init__.pyc", b"stdlib-enc"),
            ("json/__init__.pyc", b"stdlib-json"),
        ]
    )
    python_dll_bytes = (
        _construir_dll_python_con_pe_version(3, 11, 9) if incluir_pe_version_cpython else b"cpython-dll-sin-pe"
    )

    entradas: list[tuple[str, bytes, str, bool]] = [
        ("struct", b"boot-struct", "m", True),
        ("pyimod01_archive", b"boot-archive", "m", True),
        ("sky_claw/__main__", b"entry-script", "s", True),
        ("PYZ-00.pyz", pyz_bytes, "z", False),
        ("base_library.zip", base_lib_bytes, "x", False),
        ("python311.dll", python_dll_bytes, "b", True),
        ("VCRUNTIME140.dll", b"ms-vcrt-bytes", "b", True),
        ("orjson/orjson.cp311-win_amd64.pyd", b"orjson-pyd", "b", True),
        ("sky_claw/app/gui/styles.css", b"body{}", "x", True),
        ("sky_claw/app/security/security_policy.yaml", b"domains: []", "x", True),
        ("sky_claw/local/xedit/scripts/list_all_conflicts.pas", b"unit x;", "x", True),
        ("sky_claw/local/tools/synthesis_shim_fuentes/Program.cs", b"class P{}", "x", True),
        ("sky_claw/app/gui/assets/fonts/cinzel-test.woff2", b"wOF2-bytes", "x", True),
    ]
    if incluir_mo2_bridge:
        entradas.append(("sky_claw/local/mo2/plugin_bundle/skyclaw_bridge/protocol.py", b"x=1", "x", True))
    if incluir_pendientes_sbom07:
        entradas.append(("sky_claw/app/gui/assets/alduin_menace_bg.jpg", b"\xff\xd8\xff-dragon", "x", False))
        entradas.append(("sky_claw/app/gui/assets/stone_bg.png", b"\xff\xd8\xff-stone", "x", False))

    return _construir_carchive_exe(tmp_path / "SkyClawApp.exe", entradas)


# ---------------------------------------------------------------------------
# 1. Pruebas positivas (AAA) — Cobertura real, determinismo, SPDX 2.3 y SBOM-07
# ---------------------------------------------------------------------------


def test_inventario_recursivo_cubre_carchive_pyz_base_library_y_distingue_hashes(tmp_path: Path) -> None:
    # Arrange
    exe = _exe_completo_limpio(tmp_path)

    # Act
    inventario = inv.inventariar_ejecutable(exe)

    # Assert
    assert inventario["exe"]["sha256"] == hashlib.sha256(exe.read_bytes()).hexdigest()
    assert inventario["bootloader"]["bytes"] == 128
    assert len(inventario["bootloader"]["sha256"]) == 64

    contenedores = {e["container"] for e in inventario["entries"]}
    assert {"CArchive", "PYZ:PYZ-00.pyz", "ZIP:base_library.zip"} <= contenedores

    # Verifica que packed_sha256 y extracted_sha256 se distinguen cuando hay compresión
    entrada_css = next(e for e in inventario["entries"] if e["name"] == "sky_claw/app/gui/styles.css")
    assert entrada_css["packed_sha256"] != entrada_css["extracted_sha256"]
    assert entrada_css["extracted_sha256"] == hashlib.sha256(b"body{}").hexdigest()
    assert inventario["opaque_containers"] == []


def test_inventario_es_determinista_para_entradas_identicas(tmp_path: Path) -> None:
    # Arrange
    exe = _exe_completo_limpio(tmp_path)

    # Act
    inv_a = inv.inventariar_ejecutable(exe)
    inv_b = inv.inventariar_ejecutable(exe)

    # Assert
    assert json.dumps(inv_a, sort_keys=True) == json.dumps(inv_b, sort_keys=True)


def test_reconciliacion_limpia_genera_spdx_23_valido_y_distingue_instalados_vs_redistribuidos(tmp_path: Path) -> None:
    # Arrange
    exe = _exe_completo_limpio(tmp_path, incluir_mo2_bridge=False)
    inventario = inv.inventariar_ejecutable(exe)
    entorno = _entorno_limpio()
    lock = {"pydantic": "2.12.5", "orjson": "3.11.8"}  # drift deliberado en orjson (SBOM-08)

    # Act
    sbom, reporte, codigo = rec.reconciliar(
        inventario,
        entorno,
        lock_versiones=lock,
        build_python_version="3.11.9",
        creado_en="2026-11-26T18:00:00Z",
    )

    # Assert
    assert codigo == 0, reporte
    assert reporte["gate_passed"] is True
    assert rec.validar_spdx_23(sbom) == []
    assert sbom["spdxVersion"] == "SPDX-2.3"
    assert sbom["dataLicense"] == "CC0-1.0"

    # Paquetes redistribuidos vs instalados no empaquetados vs solo-build (SBOM-03)
    assert set(reporte["packages_summary"]["redistributed"]) == {"orjson", "pydantic"}
    assert "paquete-no-empaquetado" in reporte["packages_summary"]["installed_not_redistributed"]
    assert "pytest" in reporte["packages_summary"]["build_only"]

    # Drift diagnóstico respecto a requirements.lock (SBOM-08)
    assert reporte["lock_drift"] == [{"package": "orjson", "installed": "3.11.9", "locked": "3.11.8"}]


def test_sbom07_no_presume_propiedad_exclusiva_en_imagenes_ia_ni_auto_parallax_regla(tmp_path: Path) -> None:
    # Arrange
    exe = _exe_completo_limpio(tmp_path, incluir_pendientes_sbom07=True, incluir_mo2_bridge=False)
    inventario = inv.inventariar_ejecutable(exe)

    # Act
    sbom, reporte, codigo = rec.reconciliar(
        inventario,
        _entorno_limpio(),
        build_python_version="3.11.9",
        creado_en="2026-11-26T18:00:00Z",
    )

    # Assert — El gate falla cerrado específicamente por pending_legal_review (SBOM-05 + SBOM-07)
    assert codigo == 1
    assert reporte["gate_passed"] is False
    assert any(b.startswith("pending_legal_review=") for b in reporte["blockers"])
    assert rec.validar_spdx_23(sbom) == []

    pendientes = {p["name"]: p for p in reporte["pending_legal_review"]}
    assert "sky_claw/app/gui/assets/alduin_menace_bg.jpg" in pendientes
    assert "sky_claw.local.assets.auto_parallax_regla" in pendientes
    assert pendientes["sky_claw/app/gui/assets/alduin_menace_bg.jpg"]["redistribution_rights_status"] == (
        "PENDIENTE_REVISION_JURIDICA"
    )
    assert pendientes["sky_claw/app/gui/assets/alduin_menace_bg.jpg"]["technical_origin"] == "IA_DECLARADA_O_C2PA"
    assert pendientes["sky_claw.local.assets.auto_parallax_regla"]["technical_origin"] == "ADAPTACION_UPSTREAM_GPL"


# ---------------------------------------------------------------------------
# 2. Pruebas negativas de seguridad del extractor (SBOM-02) y fail-closed
# ---------------------------------------------------------------------------


def test_archivo_truncado_o_corrupto_falla_cerrado(tmp_path: Path) -> None:
    # Arrange
    exe_roto = tmp_path / "roto.exe"
    exe_roto.write_bytes(b"MZ-no-tiene-cookie-pyinstaller")

    # Act & Assert
    with pytest.raises(inv.InventarioError, match="CArchive|cookie|corrupto"):
        inv.inventariar_ejecutable(exe_roto)


def test_offset_invalido_en_toc_falla_cerrado(tmp_path: Path) -> None:
    # Arrange
    exe = _construir_carchive_exe(
        tmp_path / "offset_malo.exe",
        [("dato.txt", b"hola", "x", False)],
        forzar_offset={"dato.txt": 999_999_999},
    )

    # Act & Assert
    with pytest.raises(inv.InventarioError, match="offset|rango"):
        inv.inventariar_ejecutable(exe)


def test_entradas_duplicadas_y_colision_case_insensitive_fallan_cerrado(tmp_path: Path) -> None:
    # Arrange — colisión de mayúsculas/minúsculas en Windows
    exe = _construir_carchive_exe(
        tmp_path / "colision.exe",
        [
            ("Foo.dll", b"aaa", "b", False),
            ("foo.dll", b"bbb", "b", False),
        ],
    )

    # Act & Assert
    with pytest.raises(inv.InventarioError, match="duplicad|colisi"):
        inv.inventariar_ejecutable(exe)


def test_limite_de_descompresion_bloquea_zip_bomb(tmp_path: Path) -> None:
    # Arrange — payload muy compresible que excede el límite configurado (incluso mintiendo uncomp_len en TOC)
    bomba = b"A" * 50_000
    exe = _construir_carchive_exe(
        tmp_path / "bomba.exe",
        [("grande.dat", bomba, "x", True)],
    )
    exe_mentiroso = _construir_carchive_exe(
        tmp_path / "bomba_mentirosa.exe",
        [("grande.dat", bomba, "x", True)],
        forzar_uncompressed_len={"grande.dat": 128},
    )
    limites = inv.LimitesDeExtraccion(max_entry_bytes=4_096, max_total_bytes=8_192, max_depth=3)

    # Act & Assert
    with pytest.raises(inv.InventarioError, match="límite|descompresión|excede"):
        inv.inventariar_ejecutable(exe, limites=limites)
    with pytest.raises(inv.InventarioError, match="límite|descompresión|excede"):
        inv.inventariar_ejecutable(exe_mentiroso, limites=limites)


def test_contenedor_anidado_opaco_o_desconocido_falla_cerrado_en_reconciliacion(tmp_path: Path) -> None:
    # Arrange — archivo .7z anidado dentro de CArchive (no inspeccionable como ZIP/PYZ)
    exe = _construir_carchive_exe(
        tmp_path / "opaco.exe",
        [
            ("PYZ-00.pyz", _construir_pyz_bytes({"sky_claw": (1, b"pyc")}), "z", False),
            ("base_library.zip", _construir_zip_bytes([("json/__init__.pyc", b"j")]), "x", False),
            ("sky_claw/app/gui/styles.css", b"body{}", "x", True),
            ("sky_claw/app/security/security_policy.yaml", b"a: 1", "x", True),
            ("sky_claw/local/xedit/scripts/x.pas", b"unit x;", "x", True),
            ("sky_claw/local/tools/synthesis_shim_fuentes/Program.cs", b"class P{}", "x", True),
            ("oculto.7z", b"7z\xbc\xaf\x27\x1c-datos-opacos", "x", False),
        ],
    )
    inventario = inv.inventariar_ejecutable(exe)

    # Act
    _, reporte, codigo = rec.reconciliar(
        inventario,
        _entorno_limpio(),
        build_python_version="3.11.9",
        creado_en="2026-11-26T18:00:00Z",
    )

    # Assert
    assert codigo == 1
    assert reporte["gate_passed"] is False
    assert any(b.startswith("opaque_containers=") for b in reporte["blockers"])
    assert "oculto.7z" in [c["name"] for c in reporte["opaque_containers"]]


def test_atribucion_ambigua_de_modulo_falla_cerrado(tmp_path: Path) -> None:
    # Arrange — dos paquetes reclaman el mismo módulo top-level sin ruta RECORD desambiguadora
    exe = _construir_carchive_exe(
        tmp_path / "ambiguo.exe",
        [
            ("PYZ-00.pyz", _construir_pyz_bytes({"sky_claw": (1, b"a"), "duplicado.sub": (0, b"b")}), "z", False),
            ("base_library.zip", _construir_zip_bytes([("json/__init__.pyc", b"j")]), "x", False),
        ],
    )
    inventario = inv.inventariar_ejecutable(exe)
    entorno = _entorno_limpio() + [
        rec.PaqueteEntorno(
            nombre="duplicado-a",
            version="1.0.0",
            licencia_declarada="MIT",
            licencia_concluida="MIT",
            evidencia="METADATA dist-info",
            confianza="ALTA",
            procedencia="PyPI",
            estado_revision_juridica="VERIFICADO",
            archivos_record=(),
            modulos_top_level=("duplicado",),
            solo_build=False,
        ),
        rec.PaqueteEntorno(
            nombre="duplicado-b",
            version="2.0.0",
            licencia_declarada="MIT",
            licencia_concluida="MIT",
            evidencia="METADATA dist-info",
            confianza="ALTA",
            procedencia="PyPI",
            estado_revision_juridica="VERIFICADO",
            archivos_record=(),
            modulos_top_level=("duplicado",),
            solo_build=False,
        ),
    ]

    # Act
    _, reporte, codigo = rec.reconciliar(inventario, entorno, creado_en="2026-11-26T18:00:00Z")

    # Assert
    assert codigo == 1
    assert reporte["gate_passed"] is False
    assert any(a["name"] == "duplicado.sub" for a in reporte["ambiguous_attributions"])


def test_componente_en_exe_ausente_del_entorno_falla_cerrado(tmp_path: Path) -> None:
    # Arrange — el EXE contiene módulo "fantasma" que no existe en el entorno declarado
    exe = _construir_carchive_exe(
        tmp_path / "fantasma.exe",
        [
            ("PYZ-00.pyz", _construir_pyz_bytes({"sky_claw": (1, b"a"), "fantasma": (1, b"b")}), "z", False),
            ("base_library.zip", _construir_zip_bytes([("json/__init__.pyc", b"j")]), "x", False),
            ("sky_claw/app/gui/styles.css", b"body{}", "x", True),
            ("sky_claw/app/security/security_policy.yaml", b"a: 1", "x", True),
            ("sky_claw/local/xedit/scripts/x.pas", b"unit x;", "x", True),
            ("sky_claw/local/tools/synthesis_shim_fuentes/Program.cs", b"class P{}", "x", True),
        ],
    )
    inventario = inv.inventariar_ejecutable(exe)

    # Act
    _, reporte, codigo = rec.reconciliar(
        inventario,
        _entorno_limpio(),
        build_python_version="3.11.9",
        creado_en="2026-11-26T18:00:00Z",
    )

    # Assert
    assert codigo == 1
    assert any(b.startswith("unidentified_entries=") for b in reporte["blockers"])
    assert any(u["name"] == "fantasma" for u in reporte["unidentified"])


def test_licencia_desconocida_o_contradictoria_impide_pass_y_preserva_reporte(tmp_path: Path) -> None:
    # Arrange
    exe = _exe_completo_limpio(tmp_path)
    inv_path = tmp_path / "inventory.json"
    env_path = tmp_path / "env.json"
    sbom_path = tmp_path / "sbom.spdx.json"
    rep_path = tmp_path / "reconciliation_report.json"

    inventario = inv.inventariar_ejecutable(exe)
    inv.escribir_inventario(inventario, inv_path)

    entorno = _entorno_limpio()
    entorno[0] = rec.PaqueteEntorno(
        nombre="pydantic",
        version="2.12.5",
        licencia_declarada="NOASSERTION",
        licencia_concluida="NOASSERTION",
        evidencia="METADATA vacía",
        confianza="BAJA",
        procedencia="PyPI",
        estado_revision_juridica="PENDIENTE",
        archivos_record=("pydantic/__init__.py", "pydantic/main.py"),
        modulos_top_level=("pydantic",),
        solo_build=False,
    )
    env_path.write_text(json.dumps([p.como_dict() for p in entorno]), encoding="utf-8")

    # Act — invocación CLI real: debe fallar (exit 1) PERO conservar sbom y reporte en disco (SBOM-05)
    codigo = rec.main(
        [
            "--inventory",
            str(inv_path),
            "--env-json",
            str(env_path),
            "--sbom-out",
            str(sbom_path),
            "--report-out",
            str(rep_path),
        ]
    )

    # Assert
    assert codigo == 1
    assert sbom_path.is_file()
    assert rep_path.is_file()
    reporte = json.loads(rep_path.read_text(encoding="utf-8"))
    assert reporte["gate_passed"] is False
    assert any(x["package"] == "pydantic" for x in reporte["license_uncertainties"])


def test_validador_spdx_rechaza_sbom_vacio_o_sha_ceros() -> None:
    # Arrange — réplica del defecto histórico de v0.2.4
    sbom_v024_roto: dict[str, Any] = {
        "spdxVersion": "SPDX-2.3",
        "dataLicense": "CC0-1.0",
        "SPDXID": "SPDXRef-DOCUMENT",
        "name": "dist/",
        "documentNamespace": "https://example.invalid/sbom",
        "creationInfo": {"created": "2026-06-24T21:35:39Z", "creators": ["Tool: syft"]},
        "packages": [
            {
                "SPDXID": "SPDXRef-Package-Exe",
                "name": "SkyClawApp.exe",
                "versionInfo": "UNKNOWN",
                "downloadLocation": "NOASSERTION",
                "filesAnalyzed": False,
                "licenseConcluded": "NOASSERTION",
                "licenseDeclared": "NOASSERTION",
            }
        ],
        "files": [
            {
                "SPDXID": "SPDXRef-File-Exe",
                "fileName": "SkyClawApp.exe",
                "checksums": [{"algorithm": "SHA1", "checksumValue": "0" * 40}],
            }
        ],
        "relationships": [],
    }

    # Act
    errores = rec.validar_spdx_23(sbom_v024_roto)

    # Assert
    assert len(errores) >= 3
    assert any("SHA256" in e or "ceros" in e for e in errores)
    assert any("DESCRIBES" in e for e in errores)


def test_registro_sha256_distinto_al_exe_falla_cerrado(tmp_path: Path) -> None:
    # Arrange
    exe = _exe_completo_limpio(tmp_path)
    reg = tmp_path / "sha256.json"
    reg.write_text(json.dumps({"archivo": exe.name, "sha256": "f" * 64, "bytes": exe.stat().st_size}), encoding="utf-8")

    # Act & Assert
    with pytest.raises(inv.InventarioError, match="SHA-256"):
        inv.inventariar_ejecutable(exe, registro_sha256=reg)


def test_fallo_al_escribir_inventario_en_destino_invalido_falla_cerrado(tmp_path: Path) -> None:
    # Arrange — destino apunta a un directorio existente (no a un archivo)
    exe = _exe_completo_limpio(tmp_path)
    inventario = inv.inventariar_ejecutable(exe)

    # Act & Assert
    with pytest.raises(inv.InventarioError, match="escribir"):
        inv.escribir_inventario(inventario, tmp_path)


def test_ancla_datas_de_sky_claw_spec_cubiertos_por_clasificador() -> None:
    # Arrange — verifica coherencia con sky_claw.spec real del repo (incluyendo synthesis_shim_fuentes de main)
    spec_texto = Path("sky_claw.spec").read_text(encoding="utf-8")

    # Act & Assert
    for prefijo in inv.PREFIJOS_DATAS_ESPERADOS_SPEC:
        assert prefijo in spec_texto, f"El prefijo esperado '{prefijo}' no aparece en sky_claw.spec"
    assert "sky_claw/local/tools/synthesis_shim_fuentes" in inv.PREFIJOS_DATAS_ESPERADOS_SPEC


# ---------------------------------------------------------------------------
# 3. Pruebas adversariales R1 (F01–F06 y hallazgos Codex)
# ---------------------------------------------------------------------------


def _construir_pyz_desde_lista_toc(
    toc_raw: list[tuple[str, tuple[int, int, int]]],
    cuerpo_datos: bytes = b"",
) -> bytes:
    """Construye un PYZ crudo permitiendo duplicados o offsets arbitrarios para pruebas negativas (F06)."""
    buf = io.BytesIO()
    buf.write(b"PYZ\x00")
    buf.write(b"\xa7\x0d\x0d\x0a")
    toc_pos_placeholder = buf.tell()
    buf.write(struct.pack("!I", 0))
    buf.write(b"\x00\x00\x00\x00")
    buf.write(cuerpo_datos)
    toc_offset = buf.tell()
    buf.write(marshal.dumps(toc_raw))
    buf.seek(toc_pos_placeholder)
    buf.write(struct.pack("!I", toc_offset))
    return buf.getvalue()


def test_f01_rechaza_inventario_vacio_o_sin_entries() -> None:
    # Arrange — inventario sin entradas o con lista vacía no puede pasar el gate (F01)
    inv_vacio: dict[str, Any] = {
        "schema_version": 1,
        "exe": {"name": "SkyClawApp.exe", "bytes": 1024, "sha256": "a" * 64},
        "bootloader": {"bytes": 128, "sha256": "b" * 64},
        "opaque_containers": [],
        "entry_count": 0,
        "entries": [],
    }
    inv_sin_entries: dict[str, Any] = {
        "schema_version": 1,
        "exe": {"name": "SkyClawApp.exe", "bytes": 1024, "sha256": "a" * 64},
        "bootloader": {"bytes": 128, "sha256": "b" * 64},
    }

    # Act
    _, rep_vacio, cod_vacio = rec.reconciliar(inv_vacio, _entorno_limpio(), creado_en="2026-11-26T18:00:00Z")
    _, rep_sin, cod_sin = rec.reconciliar(inv_sin_entries, _entorno_limpio(), creado_en="2026-11-26T18:00:00Z")

    # Assert
    assert cod_vacio == 1
    assert rep_vacio["gate_passed"] is False
    assert rep_vacio["technical_inventory_complete"] is False
    assert cod_sin == 1
    assert rep_sin["gate_passed"] is False
    assert rep_sin["technical_inventory_complete"] is False


def test_f01_rechaza_entry_count_inconsistente_y_duplicados(tmp_path: Path) -> None:
    # Arrange
    exe = _exe_completo_limpio(tmp_path)
    inventario = inv.inventariar_ejecutable(exe)
    inv_mismatch = dict(inventario)
    inv_mismatch["entry_count"] = inventario["entry_count"] + 5

    inv_dup = dict(inventario)
    inv_dup["entries"] = [*inventario["entries"], inventario["entries"][0]]
    inv_dup["entry_count"] = len(inv_dup["entries"])

    # Act
    _, rep_mis, cod_mis = rec.reconciliar(inv_mismatch, _entorno_limpio(), creado_en="2026-11-26T18:00:00Z")
    _, rep_dup, cod_dup = rec.reconciliar(inv_dup, _entorno_limpio(), creado_en="2026-11-26T18:00:00Z")

    # Assert
    assert cod_mis == 1
    assert rep_mis["gate_passed"] is False
    assert rep_mis["technical_inventory_complete"] is False
    assert cod_dup == 1
    assert rep_dup["gate_passed"] is False
    assert rep_dup["technical_inventory_complete"] is False


def test_f01_rechaza_inventario_sin_componentes_estructurales_obligatorios(tmp_path: Path) -> None:
    # Arrange — EXE sintético que solo tiene una hoja CSS pero omite PYZ y base_library.zip
    exe = _construir_carchive_exe(
        tmp_path / "incompleto.exe",
        [("sky_claw/app/gui/styles.css", b"body{}", "x", True)],
    )
    inventario = inv.inventariar_ejecutable(exe)

    # Act
    _, reporte, codigo = rec.reconciliar(inventario, _entorno_limpio(), creado_en="2026-11-26T18:00:00Z")

    # Assert
    assert codigo == 1
    assert reporte["gate_passed"] is False
    assert reporte["technical_inventory_complete"] is False


def test_f02_reconcilia_analysis_toc_y_detecta_discrepancias(tmp_path: Path) -> None:
    # Arrange — analysis_toc declara un binario que no está presente en el EXE
    exe = _exe_completo_limpio(tmp_path, incluir_mo2_bridge=False)
    inventario = inv.inventariar_ejecutable(exe)
    analysis_toc = {
        "pure": [("pydantic", "/venv/lib/site-packages/pydantic/__init__.py", "PYMODULE")],
        "binaries": [("orjson/orjson.cp311-win_amd64.pyd", "/venv/lib/site-packages/orjson/orjson.pyd", "BINARY")],
        "datas": [("archivo_faltante_del_toc.dat", "/tmp/faltante.dat", "DATA")],
    }

    # Act
    _, reporte, codigo = rec.reconciliar(
        inventario,
        _entorno_limpio(),
        analysis_toc=analysis_toc,
        build_python_version="3.11.9",
        creado_en="2026-11-26T18:00:00Z",
    )

    # Assert
    assert codigo == 1
    assert reporte["gate_passed"] is False
    assert any(b.startswith("analysis_toc_missing_from_exe=") for b in reporte["blockers"])
    toc_rec = reporte["analysis_toc_reconciliation"]
    assert toc_rec["provided"] is True
    assert "archivo_faltante_del_toc.dat" in toc_rec["missing_from_exe"]


def test_f02_detecta_desajuste_entorno_python_vs_exe_y_deriva_version_del_artefacto(tmp_path: Path) -> None:
    # Arrange — el EXE declara python312 (cookie pyvers=312) pero el entorno de análisis es Python 3.11.9
    exe = _exe_completo_limpio(tmp_path)
    inventario = inv.inventariar_ejecutable(exe)
    inventario["exe"]["python_version_cookie"] = "3.12"
    inventario["exe"]["python_dll"] = "python312.dll"

    # Act
    sbom, reporte, codigo = rec.reconciliar(
        inventario,
        _entorno_limpio(),
        build_python_version="3.11.9",
        creado_en="2026-11-26T18:00:00Z",
    )

    # Assert — debe fallar cerrado por desajuste de entorno y NO estampar 3.11.9 como versión de CPython del EXE
    assert codigo == 1
    assert reporte["gate_passed"] is False
    assert reporte["environment_mismatch"]
    pkg_py = next(p for p in sbom["packages"] if p["name"] == "cpython-runtime")
    assert pkg_py["versionInfo"] != "3.11.9"


def test_f02_verifica_hash_record_binario_sin_exigir_igualdad_en_bytecode(tmp_path: Path) -> None:
    # Arrange — orjson.pyd tiene hash RECORD que NO coincide con el binario empaquetado; pydantic solo es bytecode PYZ
    exe = _exe_completo_limpio(tmp_path)
    inventario = inv.inventariar_ejecutable(exe)
    entorno = _entorno_limpio()
    entorno[1] = rec.PaqueteEntorno(
        nombre="orjson",
        version="3.11.9",
        licencia_declarada="MPL-2.0 AND (Apache-2.0 OR MIT)",
        licencia_concluida="MPL-2.0 AND (Apache-2.0 OR MIT)",
        evidencia="METADATA dist-info",
        confianza="ALTA",
        procedencia="PyPI",
        estado_revision_juridica="VERIFICADO_DOCUMENTAL",
        archivos_record=("orjson/__init__.py", "orjson/orjson.cp311-win_amd64.pyd"),
        modulos_top_level=("orjson",),
        solo_build=False,
        record_hashes={
            "orjson/__init__.py": "0" * 64,  # fuente .py: NO debe compararse contra bytecode PYZ
            "orjson/orjson.cp311-win_amd64.pyd": "f" * 64,  # binario .pyd: SÍ debe coincidir y fallar aquí
        },
    )

    # Act
    _, reporte, codigo = rec.reconciliar(inventario, entorno, creado_en="2026-11-26T18:00:00Z")

    # Assert
    assert codigo == 1
    assert reporte["gate_passed"] is False
    mismatches = reporte["hash_mismatches"]
    assert any(m["name"] == "orjson/orjson.cp311-win_amd64.pyd" for m in mismatches)
    assert not any(m["name"] == "orjson" for m in mismatches)


def test_f03_separa_nativos_fuentes_por_familia_vendorizados_y_pendientes_en_spdx(tmp_path: Path) -> None:
    # Arrange — EXE con OpenSSL, SQLite, libffi, Tcl/Tk, MS runtime, 2 familias de fuentes, nicegui JS y activo IA
    pyz_bytes = _construir_pyz_bytes({"sky_claw": (1, b"pyc-s"), "pydantic": (1, b"pyc-p")})
    base_lib = _construir_zip_bytes([("json/__init__.pyc", b"j")])
    exe = _construir_carchive_exe(
        tmp_path / "componentes_f03.exe",
        [
            ("struct", b"boot-s", "m", True),
            ("PYZ-00.pyz", pyz_bytes, "z", False),
            ("base_library.zip", base_lib, "x", False),
            ("python311.dll", b"cpython", "b", True),
            ("libcrypto-3-x64.dll", b"openssl-crypto", "b", True),
            ("libssl-3-x64.dll", b"openssl-ssl", "b", True),
            ("sqlite3.dll", b"sqlite-bin", "b", True),
            ("libffi-8.dll", b"libffi-bin", "b", True),
            ("tcl86t.dll", b"tcl-bin", "b", True),
            ("VCRUNTIME140.dll", b"ms-vcrt", "b", True),
            ("sky_claw/app/gui/assets/fonts/cinzel-400.woff2", b"wOF2-c", "x", True),
            ("sky_claw/app/gui/assets/fonts/eb-garamond-400.woff2", b"wOF2-g", "x", True),
            ("nicegui/static/quasar.umd.prod.js", b"var q=1;", "x", True),
            ("sky_claw/app/gui/assets/alduin_menace_bg.jpg", b"\xff\xd8\xff-dragon", "x", False),
        ],
    )
    inventario = inv.inventariar_ejecutable(exe)

    # Act
    sbom, reporte, codigo = rec.reconciliar(inventario, _entorno_limpio(), creado_en="2026-11-26T18:00:00Z")

    # Assert — cada componente nativo, familia de fuente, vendorizado y recurso pendiente aparece separado en SPDX
    assert codigo == 1  # hay recurso pendiente de revisión jurídica y vendorizado sin licencia verificada
    nombres_spdx = {p["name"] for p in sbom["packages"]}
    assert "cpython-runtime" in nombres_spdx
    assert "openssl-embedded" in nombres_spdx
    assert "sqlite-embedded" in nombres_spdx
    assert "libffi-embedded" in nombres_spdx
    assert "tcl-tk-embedded" in nombres_spdx
    assert "microsoft-windows-runtime" in nombres_spdx
    assert "sky-claw-font-cinzel" in nombres_spdx
    assert "sky-claw-font-eb-garamond" in nombres_spdx
    assert "nicegui-vendored-frontend-assets" in nombres_spdx
    assert any("alduin_menace_bg.jpg" in p["name"] for p in sbom["packages"])
    assert rec.validar_spdx_23(sbom) == []


def test_f04_no_copia_declared_a_concluded_y_normaliza_clasificadores_legacy() -> None:
    # Arrange & Act — clasificador ambiguo "BSD License" NO puede emitirse como expresión SPDX
    decl_amb, ev_amb, _ = rec._normalizar_licencia_metadata("", "", ["License :: OSI Approved :: BSD License"])
    decl_mit, _, _ = rec._normalizar_licencia_metadata("", "", ["License :: OSI Approved :: MIT License"])
    paquetes = rec.recolectar_entorno_actual()

    # Assert
    assert decl_amb == "NOASSERTION"
    assert "BSD License" in ev_amb
    assert decl_mit == "MIT"
    assert paquetes, "El entorno actual debe tener paquetes instalados"
    for pkg in paquetes:
        assert pkg.licencia_concluida == "NOASSERTION"
        assert pkg.estado_revision_juridica != "VERIFICADO"


def test_f05_validador_spdx_rechaza_relacion_huerfana_y_expresion_licencia_invalida(tmp_path: Path) -> None:
    # Arrange
    exe = _exe_completo_limpio(tmp_path)
    inventario = inv.inventariar_ejecutable(exe)
    sbom, _, _ = rec.reconciliar(inventario, _entorno_limpio(), creado_en="2026-11-26T18:00:00Z")
    sbom["packages"][0]["licenseDeclared"] = "BSD License"  # prosa inválida, no es expresión SPDX
    sbom["relationships"].append(
        {
            "spdxElementId": "SPDXRef-Package-SkyClawApp-Exe",
            "relationshipType": "CONTAINS",
            "relatedSpdxElement": "SPDXRef-Inexistente",
        }
    )

    # Act
    errores = rec.validar_spdx_23(sbom)

    # Assert
    assert any("licencia" in e.lower() or "spdx" in e.lower() for e in errores)
    assert any("SPDXRef-Inexistente" in e for e in errores)


def test_f06_aplica_max_pyz_toc_bytes_duplicados_y_offsets_en_pyz(tmp_path: Path) -> None:
    # Arrange 1 — PYZ con TOC que excede max_pyz_toc_bytes
    pyz_grande = _construir_pyz_bytes({f"mod_{i}": (0, b"x") for i in range(40)})
    exe_toc = _construir_carchive_exe(tmp_path / "pyz_toc_grande.exe", [("PYZ-00.pyz", pyz_grande, "z", False)])
    lim_toc = inv.LimitesDeExtraccion(max_pyz_toc_bytes=64)

    # Arrange 2 — PYZ con entrada duplicada antes de convertir a dict
    cuerpo = zlib.compress(b"abc")
    pyz_dup = _construir_pyz_desde_lista_toc(
        [("dup_mod", (0, 16, len(cuerpo))), ("DUP_MOD", (0, 16, len(cuerpo)))],
        cuerpo_datos=cuerpo,
    )
    exe_dup = _construir_carchive_exe(tmp_path / "pyz_dup.exe", [("PYZ-00.pyz", pyz_dup, "z", False)])

    # Arrange 3 — PYZ con offset fuera de rango
    pyz_bad_off = _construir_pyz_desde_lista_toc([("malo", (0, 999999, 10))], cuerpo_datos=cuerpo)
    exe_off = _construir_carchive_exe(tmp_path / "pyz_off.exe", [("PYZ-00.pyz", pyz_bad_off, "z", False)])

    # Act & Assert
    with pytest.raises(inv.InventarioError, match="TOC|PYZ|límite"):
        inv.inventariar_ejecutable(exe_toc, limites=lim_toc)
    with pytest.raises(inv.InventarioError, match="duplicad|colisi"):
        inv.inventariar_ejecutable(exe_dup)
    with pytest.raises(inv.InventarioError, match="offset|rango|PYZ"):
        inv.inventariar_ejecutable(exe_off)


def test_f06_rechaza_zlib_truncado_bytes_sobrantes_y_tamano_declarado_distinto(tmp_path: Path) -> None:
    # Arrange & Act & Assert 1 — flujo zlib truncado (sin checksum Adler-32 final)
    truncado = zlib.compress(b"payload-de-prueba")[:-4]
    with pytest.raises(inv.InventarioError, match="incompleto|truncado|eof|zlib"):
        inv._descomprimir_acotado(truncado, 1024, "truncado")

    # Arrange & Act & Assert 2 — bytes sobrantes tras el fin del flujo zlib
    sobrante = zlib.compress(b"payload-de-prueba") + b"BASURA"
    with pytest.raises(inv.InventarioError, match="sobrante|trailing|unused|zlib"):
        inv._descomprimir_acotado(sobrante, 1024, "sobrante")

    # Arrange & Act & Assert 3 — tamaño descomprimido declarado en TOC no coincide con extraído
    exe_len = _construir_carchive_exe(
        tmp_path / "len_distinto.exe",
        [("dato.txt", b"hola-mundo", "x", True)],
        forzar_uncompressed_len={"dato.txt": 999},
    )
    with pytest.raises(inv.InventarioError, match="tamaño|declarado|extraído"):
        inv.inventariar_ejecutable(exe_len)


def test_f06_aplica_max_entry_count_y_no_clasifica_prefijo_pyi_generico_como_bootloader(tmp_path: Path) -> None:
    # Arrange 1 — límite de cantidad de entradas
    exe = _exe_completo_limpio(tmp_path)
    lim_cnt = inv.LimitesDeExtraccion(max_entry_count=3)
    with pytest.raises(inv.InventarioError, match="entradas|límite|max_entry_count"):
        inv.inventariar_ejecutable(exe, limites=lim_cnt)

    # Arrange 2 — módulo de tercero cuyo nombre empieza con "pyi" NO es bootloader de PyInstaller
    cat, orig, _ = inv._clasificar_entrada_base("pyiceberg", "m", "CArchive")
    assert cat != inv.CategoriaComponente.PYINSTALLER_BOOTLOADER.value
    assert orig != inv.OrigenTecnico.PYINSTALLER_BOOTLOADER.value


def test_f06_no_reutiliza_informe_pass_obsoleto_tras_fallo_cli(tmp_path: Path) -> None:
    # Arrange — existe un informe PASS previo en disco y ahora la invocación CLI falla con entrada corrupta
    inv_roto = tmp_path / "roto.json"
    inv_roto.write_text("{json-invalido", encoding="utf-8")
    sbom_path = tmp_path / "sbom.spdx.json"
    rep_path = tmp_path / "reconciliation_report.json"
    sbom_path.write_text('{"stale": true}', encoding="utf-8")
    rep_path.write_text('{"gate_passed": true}', encoding="utf-8")

    # Act
    codigo = rec.main(
        [
            "--inventory",
            str(inv_roto),
            "--sbom-out",
            str(sbom_path),
            "--report-out",
            str(rep_path),
        ]
    )

    # Assert
    assert codigo == rec.EXIT_ERROR
    assert not rep_path.exists()
    assert not sbom_path.exists()


def test_f06_soporta_paquete_namespace_pep420_vacio_en_pyz(tmp_path: Path) -> None:
    # Arrange — en PyInstaller 6, PYZ_ITEM_NSPKG (ispkg=3) tiene mod_len=0 sin flujo zlib
    pyz_ns = _construir_pyz_desde_lista_toc([("google", (3, 16, 0))], cuerpo_datos=b"")
    exe_ns = _construir_carchive_exe(tmp_path / "ns_pep420.exe", [("PYZ-00.pyz", pyz_ns, "z", False)])

    # Act
    inventario = inv.inventariar_ejecutable(exe_ns)

    # Assert
    entrada_google = next(e for e in inventario["entries"] if e["name"] == "google")
    assert entrada_google["typecode"] == "pyz_pkg"
    assert entrada_google["extracted_bytes"] == 0


# ---------------------------------------------------------------------------
# 4. Pruebas adversariales Addendum R1 (F08, F09, F10)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ruta_maliciosa",
    [
        "../sky_claw/app.py",
        "./../sky_claw/app.py",
        "a/../../b.dll",
        "/etc/passwd",
        "\\Windows\\System32\\evil.dll",
        "C:\\Windows\\evil.dll",
        "D:/sky_claw/app.py",
        "\\\\servidor\\recurso\\evil.dll",
        "//servidor/recurso/evil.dll",
        "foo\\..\\bar.dll",
    ],
)
def test_f08_rechaza_rutas_inseguras_en_carchive_y_zip_antes_de_normalizar(tmp_path: Path, ruta_maliciosa: str) -> None:
    # Arrange — probar tanto en CArchive como en ZIP anidado
    exe_carch = _construir_carchive_exe(
        tmp_path / "malo_carchive.exe",
        [(ruta_maliciosa, b"payload", "x", False)],
    )
    zip_malo = _construir_zip_bytes([(ruta_maliciosa, b"payload")])
    exe_zip = _construir_carchive_exe(
        tmp_path / "malo_zip.exe",
        [("base_library.zip", zip_malo, "x", False)],
    )

    # Act & Assert
    with pytest.raises(inv.InventarioError, match="ruta|traversal|absoluta|unidad|UNC|inválid"):
        inv.inventariar_ejecutable(exe_carch)
    with pytest.raises(inv.InventarioError, match="ruta|traversal|absoluta|unidad|UNC|inválid"):
        inv.inventariar_ejecutable(exe_zip)


def test_f08_rechaza_colision_post_normalizacion_con_separadores_mixtos(tmp_path: Path) -> None:
    # Arrange — dos entradas distintas en bytes crudos que colisionan tras normalizar separadores
    exe_mix = _construir_carchive_exe(
        tmp_path / "colision_mixta.exe",
        [
            ("pkg\\sub/archivo.dll", b"aaa", "b", False),
            ("pkg/sub\\ARCHIVO.dll", b"bbb", "b", False),
        ],
    )

    # Act & Assert
    with pytest.raises(inv.InventarioError, match="duplicad|colisi"):
        inv.inventariar_ejecutable(exe_mix)


def test_f09_representa_skyclaw_bridge_y_frontera_mo2_in_process_en_inventario_y_sbom(tmp_path: Path) -> None:
    # Arrange
    exe = _exe_completo_limpio(tmp_path)
    inventario = inv.inventariar_ejecutable(exe)

    # Act
    sbom, reporte, codigo = rec.reconciliar(
        inventario,
        _entorno_limpio(),
        build_python_version="3.11.9",
        creado_en="2026-11-26T18:00:00Z",
    )

    # Assert — skyclaw_bridge aparece como componente identificable con frontera MO2 y revisión jurídica pendiente
    entrada_bridge = next(e for e in inventario["entries"] if "skyclaw_bridge/protocol.py" in e["name"])
    assert entrada_bridge["technical_origin"] == inv.OrigenTecnico.MO2_PLUGIN_IN_PROCESS.value
    assert (
        entrada_bridge["redistribution_rights_status"]
        == inv.EstadoDerechosRedistribucion.PENDIENTE_REVISION_JURIDICA.value
    )

    pkg_mo2 = next((p for p in sbom["packages"] if p["name"] == "skyclaw-mo2-bridge-plugin"), None)
    assert pkg_mo2 is not None
    assert pkg_mo2["licenseConcluded"] == "NOASSERTION"
    assert "MO2" in pkg_mo2.get("comment", "")

    frontera = reporte["mo2_integration_boundary"]
    assert frontera["present"] is True
    assert frontera["boundary_type"] == "IN_PROCESS_MO2_PLUGIN"
    assert frontera["legal_review_status"] == "PENDIENTE_REVISION_JURIDICA"
    assert frontera["compatibility_assessment"] == "NOASSERTION"
    assert codigo == 1
    assert rec.validar_spdx_23(sbom) == []


def test_f10_identidad_cpython_no_usa_sys_version_info_y_bloquea_version_exacta_ausente(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange — el intérprete inspector corre en 3.12.9 pero el artefacto y build son 3.11.9
    from typing import NamedTuple

    class _FakeVersionInfo(NamedTuple):
        major: int = 3
        minor: int = 12
        micro: int = 9
        releaselevel: str = "final"
        serial: int = 0

    monkeypatch.setattr(rec.sys, "version_info", _FakeVersionInfo())
    exe = _exe_completo_limpio(tmp_path)
    inventario = inv.inventariar_ejecutable(exe)

    # Act 1 — con versión exacta de build vinculada (3.11.9) NO debe tomar 3.12.9 del inspector
    sbom_ok, rep_ok, _ = rec.reconciliar(
        inventario,
        _entorno_limpio(),
        build_python_version="3.11.9",
        creado_en="2026-11-26T18:00:00Z",
    )
    pkg_py_ok = next(p for p in sbom_ok["packages"] if p["name"] == "cpython-runtime")
    assert pkg_py_ok["versionInfo"] == "3.11.9"
    assert rep_ok["cpython_version_uncertainty"] is False

    # Act 2 — sin versión exacta de build vinculada, debe informar incertidumbre y bloquear
    sbom_inc, rep_inc, cod_inc = rec.reconciliar(
        inventario,
        _entorno_limpio(),
        creado_en="2026-11-26T18:00:00Z",
    )
    pkg_py_inc = next(p for p in sbom_inc["packages"] if p["name"] == "cpython-runtime")
    assert pkg_py_inc["versionInfo"] == "NOASSERTION"
    assert rep_inc["cpython_version_uncertainty"] is True
    assert cod_inc == 1
    assert any("cpython_version_unverified" in b for b in rep_inc["blockers"])


# ---------------------------------------------------------------------------
# 5. Pruebas adversariales R2 (F11, F12, F13)
# ---------------------------------------------------------------------------


def _construir_dll_python_con_pe_version(major: int, minor: int, micro: int) -> bytes:
    """Construye un bloque binario sintético con recurso PE VS_FIXEDFILEINFO estilo CPython Windows."""
    ms = (major << 16) | minor
    ls = ((micro * 1000 + 150) << 16) | 1013  # CPython final release encoding
    fixed_info = struct.pack("<IIII", 0xFEEF04BD, 0x00010000, ms, ls)
    return b"MZ" + b"\x00" * 64 + fixed_info + b"\x00" * 16


def test_f11_distingue_inventario_estructural_de_reconciliacion_de_procedencia_sin_analysis_toc(
    tmp_path: Path,
) -> None:
    # Arrange
    exe = _exe_completo_limpio(tmp_path, incluir_mo2_bridge=False)
    inventario = inv.inventariar_ejecutable(exe)

    # Act 1 — sin analysis_toc: inventario estructural OK, pero procedencia NO verificada
    _, rep_sin_toc, _ = rec.reconciliar(
        inventario,
        _entorno_limpio(),
        creado_en="2026-11-26T18:00:00Z",
    )

    # Act 2 — con analysis_toc vacío: debe fallar cerrado (EMPTY_TOC)
    _, rep_toc_vacio, cod_toc_vacio = rec.reconciliar(
        inventario,
        _entorno_limpio(),
        analysis_toc={"pure": [], "binaries": [], "datas": []},
        creado_en="2026-11-26T18:00:00Z",
    )

    # Act 3 — con analysis_toc válido coincidente: procedencia verificada
    _, rep_con_toc, _ = rec.reconciliar(
        inventario,
        _entorno_limpio(),
        analysis_toc={
            "pure": [("pydantic", "/venv/pydantic/__init__.py", "PYMODULE")],
            "binaries": [("orjson/orjson.cp311-win_amd64.pyd", "/venv/orjson.pyd", "BINARY")],
        },
        creado_en="2026-11-26T18:00:00Z",
    )

    # Assert
    assert rep_sin_toc["structural_inventory_complete"] is True
    assert rep_sin_toc["provenance_reconciliation_verified"] is False
    assert rep_sin_toc["analysis_toc_reconciliation"]["status"] == "NOT_PROVIDED"

    assert cod_toc_vacio == 1
    assert rep_toc_vacio["provenance_reconciliation_verified"] is False
    assert rep_toc_vacio["analysis_toc_reconciliation"]["status"] == "EMPTY_TOC"

    assert rep_con_toc["provenance_reconciliation_verified"] is True
    assert rep_con_toc["analysis_toc_reconciliation"]["status"] == "VERIFIED"


def test_f12_no_certifica_patch_cpython_solo_por_build_python_version_sin_evidencia_en_artefacto(
    tmp_path: Path,
) -> None:
    # Arrange 1 — DLL sin VS_FIXEDFILEINFO: pasar --build-python-version=3.11.99 NO puede certificar patch
    exe_sin_pe = _exe_completo_limpio(tmp_path, incluir_mo2_bridge=False, incluir_pe_version_cpython=False)
    inv_sin_pe = inv.inventariar_ejecutable(exe_sin_pe)

    sbom_no_probado, rep_no_probado, cod_no_probado = rec.reconciliar(
        inv_sin_pe,
        _entorno_limpio(),
        build_python_version="3.11.99",
        creado_en="2026-11-26T18:00:00Z",
    )

    # Assert 1 — sin evidencia en el artefacto, rechaza certificar 3.11.99
    pkg_py_no = next(p for p in sbom_no_probado["packages"] if p["name"] == "cpython-runtime")
    assert pkg_py_no["versionInfo"] == "NOASSERTION"
    assert rep_no_probado["cpython_version_uncertainty"] is True
    assert cod_no_probado == 1
    assert any("cpython_version_unverified" in b for b in rep_no_probado["blockers"])

    # Arrange 2 — DLL con VS_FIXEDFILEINFO 3.11.9 real: certifica 3.11.9 y rechaza 3.11.99 por mismatch de patch
    dll_3119 = _construir_dll_python_con_pe_version(3, 11, 9)
    pyz_bytes = _construir_pyz_bytes({"sky_claw": (1, b"s"), "pydantic": (1, b"p")})
    base_lib = _construir_zip_bytes([("json/__init__.pyc", b"j")])
    exe_con_pe = _construir_carchive_exe(
        tmp_path / "con_pe.exe",
        [
            ("PYZ-00.pyz", pyz_bytes, "z", False),
            ("base_library.zip", base_lib, "x", False),
            ("python311.dll", dll_3119, "b", True),
        ],
    )
    inv_con_pe = inv.inventariar_ejecutable(exe_con_pe)
    assert inv_con_pe["exe"]["python_version_binary"] == "3.11.9"

    sbom_ok, rep_ok, cod_ok = rec.reconciliar(
        inv_con_pe,
        _entorno_limpio(),
        build_python_version="3.11.9",
        creado_en="2026-11-26T18:00:00Z",
    )
    pkg_py_ok = next(p for p in sbom_ok["packages"] if p["name"] == "cpython-runtime")
    assert pkg_py_ok["versionInfo"] == "3.11.9"
    assert rep_ok["cpython_version_uncertainty"] is False
    assert cod_ok == 0

    _, rep_patch_mal, cod_patch_mal = rec.reconciliar(
        inv_con_pe,
        _entorno_limpio(),
        build_python_version="3.11.99",
        creado_en="2026-11-26T18:00:00Z",
    )
    assert cod_patch_mal == 1
    assert rep_patch_mal["environment_mismatch"]


def test_f13_distingue_atribuciones_criptograficas_nominales_y_bytecode_en_cobertura_record(
    tmp_path: Path,
) -> None:
    # Arrange — 1 binario con SHA-256 RECORD verificado, 1 dato nominal sin hash, y módulos PYZ bytecode
    orjson_bytes = b"orjson-pyd-exact"
    orjson_sha = hashlib.sha256(orjson_bytes).hexdigest()
    pyz_bytes = _construir_pyz_bytes({"sky_claw": (1, b"s"), "pydantic": (1, b"p"), "orjson": (1, b"o")})
    base_lib = _construir_zip_bytes([("json/__init__.pyc", b"j")])
    exe = _construir_carchive_exe(
        tmp_path / "f13.exe",
        [
            ("PYZ-00.pyz", pyz_bytes, "z", False),
            ("base_library.zip", base_lib, "x", False),
            ("python311.dll", _construir_dll_python_con_pe_version(3, 11, 9), "b", True),
            ("orjson/orjson.cp311-win_amd64.pyd", orjson_bytes, "b", True),
            ("pydantic/dato_sin_hash.json", b"{}", "x", True),
        ],
    )
    inventario = inv.inventariar_ejecutable(exe)
    entorno = _entorno_limpio()
    entorno[1] = rec.PaqueteEntorno(
        nombre="orjson",
        version="3.11.9",
        licencia_declarada="MPL-2.0 AND (Apache-2.0 OR MIT)",
        licencia_concluida="MPL-2.0 AND (Apache-2.0 OR MIT)",
        evidencia="METADATA dist-info",
        confianza="ALTA",
        procedencia="PyPI",
        estado_revision_juridica="VERIFICADO_DOCUMENTAL",
        archivos_record=("orjson/__init__.py", "orjson/orjson.cp311-win_amd64.pyd"),
        modulos_top_level=("orjson",),
        solo_build=False,
        record_hashes={"orjson/orjson.cp311-win_amd64.pyd": orjson_sha},
    )

    # Act
    _, reporte, _ = rec.reconciliar(
        inventario,
        entorno,
        build_python_version="3.11.9",
        creado_en="2026-11-26T18:00:00Z",
    )

    # Assert — separación explícita y suma exacta de cobertura RECORD
    hv = reporte["hash_verification"]
    assert hv["verified_cryptographic_record_matches"] == 1
    assert hv["nominal_attributions_without_hash"] == 1
    assert "pydantic/dato_sin_hash.json" in hv["nominal_entries_sample"]
    assert hv["bytecode_transformed_not_hash_comparable"] == 2
    assert hv["total_attributed_entries"] == (
        hv["verified_cryptographic_record_matches"]
        + hv["nominal_attributions_without_hash"]
        + hv["bytecode_transformed_not_hash_comparable"]
        + len(hv["mismatches"])
    )

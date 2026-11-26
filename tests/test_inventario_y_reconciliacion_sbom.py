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


def _exe_completo_limpio(tmp_path: Path, *, incluir_pendientes_sbom07: bool = False) -> Path:
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

    entradas: list[tuple[str, bytes, str, bool]] = [
        ("struct", b"boot-struct", "m", True),
        ("pyimod01_archive", b"boot-archive", "m", True),
        ("sky_claw/__main__", b"entry-script", "s", True),
        ("PYZ-00.pyz", pyz_bytes, "z", False),
        ("base_library.zip", base_lib_bytes, "x", False),
        ("python311.dll", b"cpython-dll-bytes", "b", True),
        ("VCRUNTIME140.dll", b"ms-vcrt-bytes", "b", True),
        ("orjson/orjson.cp311-win_amd64.pyd", b"orjson-pyd", "b", True),
        ("sky_claw/app/gui/styles.css", b"body{}", "x", True),
        ("sky_claw/app/security/security_policy.yaml", b"domains: []", "x", True),
        ("sky_claw/local/xedit/scripts/list_all_conflicts.pas", b"unit x;", "x", True),
        ("sky_claw/local/tools/synthesis_shim_fuentes/Program.cs", b"class P{}", "x", True),
        ("sky_claw/local/mo2/plugin_bundle/skyclaw_bridge/protocol.py", b"x=1", "x", True),
        ("sky_claw/app/gui/assets/fonts/cinzel-test.woff2", b"wOF2-bytes", "x", True),
    ]
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
    exe = _exe_completo_limpio(tmp_path)
    inventario = inv.inventariar_ejecutable(exe)
    entorno = _entorno_limpio()
    lock = {"pydantic": "2.12.5", "orjson": "3.11.8"}  # drift deliberado en orjson (SBOM-08)

    # Act
    sbom, reporte, codigo = rec.reconciliar(
        inventario,
        entorno,
        lock_versiones=lock,
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
    exe = _exe_completo_limpio(tmp_path, incluir_pendientes_sbom07=True)
    inventario = inv.inventariar_ejecutable(exe)

    # Act
    sbom, reporte, codigo = rec.reconciliar(
        inventario,
        _entorno_limpio(),
        creado_en="2026-11-26T18:00:00Z",
    )

    # Assert — El gate falla cerrado por incertidumbre jurídica material (SBOM-05 + SBOM-07)
    assert codigo == 1
    assert reporte["gate_passed"] is False
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
    # Arrange — payload muy compresible que excede el límite configurado
    bomba = b"A" * 50_000
    exe = _construir_carchive_exe(
        tmp_path / "bomba.exe",
        [("grande.dat", bomba, "x", True)],
    )
    limites = inv.LimitesDeExtraccion(max_entry_bytes=4_096, max_total_bytes=8_192, max_depth=3)

    # Act & Assert
    with pytest.raises(inv.InventarioError, match="límite|descompresión|excede"):
        inv.inventariar_ejecutable(exe, limites=limites)


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
            ("sky_claw/local/mo2/plugin_bundle/skyclaw_bridge/protocol.py", b"x=1", "x", True),
            ("oculto.7z", b"7z\xbc\xaf\x27\x1c-datos-opacos", "x", False),
        ],
    )
    inventario = inv.inventariar_ejecutable(exe)

    # Act
    _, reporte, codigo = rec.reconciliar(inventario, _entorno_limpio(), creado_en="2026-11-26T18:00:00Z")

    # Assert
    assert codigo == 1
    assert reporte["gate_passed"] is False
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
            ("sky_claw/local/mo2/plugin_bundle/skyclaw_bridge/protocol.py", b"x=1", "x", True),
        ],
    )
    inventario = inv.inventariar_ejecutable(exe)

    # Act
    _, reporte, codigo = rec.reconciliar(inventario, _entorno_limpio(), creado_en="2026-11-26T18:00:00Z")

    # Assert
    assert codigo == 1
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

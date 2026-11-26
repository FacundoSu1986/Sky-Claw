"""Inventario determinista, recursivo y fail-closed de un ejecutable PyInstaller (PR-LIC-03A).

Inspecciona en modo estrictamente pasivo (sin ejecutar el EXE ni importar módulos extraídos):
- Segmento de bootloader nativo de PyInstaller (bytes anteriores a ``CArchive``).
- ``CArchive`` (via ``PyInstaller.archive.readers.CArchiveReader`` con validación previa de
  cookie, offsets, límites de descompresión, duplicados y colisiones insensibles a mayúsculas).
- ``PYZ`` embebido (via ``ZlibArchiveReader`` con cota estricta del TOC).
- ``base_library.zip`` y contenedores ``.zip``/``.whl``/``.egg`` anidados hasta ``max_depth``.
- Detección explícita de contenedores opacos no inspeccionables (``.7z``, ``.tar``, ``.rar``,
  ``.cab``, ``PKG_ITEM_ZIPFILE``) para fallo cerrado en reconciliación (SBOM-01).

Distingue siempre ``packed_sha256`` (bytes crudos/comprimidos en el contenedor) y
``extracted_sha256`` (bytes descomprimidos) conforme a SBOM-04, y separa categoría técnica,
origen técnico y estado de derechos de redistribución conforme a SBOM-07.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import struct
import sys
import zipfile
import zlib
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from PyInstaller.archive.readers import (
    PKG_ITEM_BINARY,
    PKG_ITEM_DATA,
    PKG_ITEM_DEPENDENCY,
    PKG_ITEM_PYMODULE,
    PKG_ITEM_PYPACKAGE,
    PKG_ITEM_PYSOURCE,
    PKG_ITEM_PYZ,
    PKG_ITEM_RUNTIME_OPTION,
    PKG_ITEM_SPLASH,
    PKG_ITEM_ZIPFILE,
    ArchiveReadError,
    CArchiveReader,
)

EXIT_OK = 0
EXIT_VALIDACION = 1
EXIT_ERROR = 2

_TAMANO_BLOQUE = 1024 * 1024
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

#: Prefijos exactos de ``datas`` declarados en ``sky_claw.spec`` (alineado con origin/main 2a93bb18).
PREFIJOS_DATAS_ESPERADOS_SPEC: tuple[str, ...] = (
    "sky_claw/app/gui/styles.css",
    "sky_claw/app/gui/assets",
    "sky_claw/local/xedit/scripts",
    "sky_claw/local/tools/synthesis_shim_fuentes",
    "sky_claw/local/mo2/plugin_bundle/skyclaw_bridge",
    "sky_claw/app/security/security_policy.yaml",
)

#: Activos que NO pueden presumirse propiedad exclusiva del titular sin revisión jurídica (SBOM-07).
_ACTIVOS_IA_PENDIENTES = frozenset(
    {
        "sky_claw/app/gui/assets/alduin_menace_bg.jpg",
        "sky_claw/app/gui/assets/stone_bg.png",
        "sky_claw/app/gui/assets/parchment.png",
    }
)
_MODULOS_ADAPTACION_UPSTREAM_PENDIENTES = frozenset({"sky_claw.local.assets.auto_parallax_regla"})

_EXTENSIONES_ZIP = frozenset({".zip", ".whl", ".egg"})
_EXTENSIONES_OPACAS = frozenset({".7z", ".rar", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".cab"})
_DLLS_SISTEMA_WINDOWS_PREFIJOS = ("api-ms-win-", "ext-ms-win-")
_DLLS_SISTEMA_WINDOWS_EXACTAS = frozenset(
    {
        "ucrtbase.dll",
        "vcruntime140.dll",
        "vcruntime140_1.dll",
        "msvcp140.dll",
        "msvcp140_1.dll",
        "msvcp140_2.dll",
        "concrt140.dll",
        "vcomp140.dll",
    }
)
_DLLS_CPYTHON_STDLIB = frozenset(
    {
        "python3.dll",
        "sqlite3.dll",
        "libffi-8.dll",
        "libffi-7.dll",
        "libcrypto-3.dll",
        "libcrypto-3-x64.dll",
        "libssl-3.dll",
        "libssl-3-x64.dll",
        "tcl86t.dll",
        "tk86t.dll",
    }
)
_TYPECODES_VALIDOS = frozenset(
    {
        PKG_ITEM_BINARY,
        PKG_ITEM_DATA,
        PKG_ITEM_DEPENDENCY,
        PKG_ITEM_PYMODULE,
        PKG_ITEM_PYPACKAGE,
        PKG_ITEM_PYSOURCE,
        PKG_ITEM_PYZ,
        PKG_ITEM_RUNTIME_OPTION,
        PKG_ITEM_SPLASH,
        PKG_ITEM_ZIPFILE,
    }
)


class InventarioError(ValueError):
    """Error fail-closed durante la lectura, validación o escritura del inventario."""


class CategoriaComponente(StrEnum):
    FIRST_PARTY_CODE = "FIRST_PARTY_CODE"
    FIRST_PARTY_ASSET = "FIRST_PARTY_ASSET"
    THIRD_PARTY_FONT = "THIRD_PARTY_FONT"
    THIRD_PARTY_PYTHON = "THIRD_PARTY_PYTHON"
    THIRD_PARTY_NATIVE = "THIRD_PARTY_NATIVE"
    PYINSTALLER_BOOTLOADER = "PYINSTALLER_BOOTLOADER"
    WINDOWS_SYSTEM_DLL = "WINDOWS_SYSTEM_DLL"
    THIRD_PARTY_VENDORED = "THIRD_PARTY_VENDORED"
    OPAQUE_CONTAINER = "OPAQUE_CONTAINER"
    UNIDENTIFIED = "UNIDENTIFIED"


class OrigenTecnico(StrEnum):
    REPO_PROPIO = "REPO_PROPIO"
    IA_DECLARADA_O_C2PA = "IA_DECLARADA_O_C2PA"
    ADAPTACION_UPSTREAM_GPL = "ADAPTACION_UPSTREAM_GPL"
    GOOGLE_FONTS_OFL = "GOOGLE_FONTS_OFL"
    PYPI_DISTRIBUCION = "PYPI_DISTRIBUCION"
    CPYTHON_STDLIB = "CPYTHON_STDLIB"
    PYINSTALLER_BOOTLOADER = "PYINSTALLER_BOOTLOADER"
    WINDOWS_OS_RUNTIME = "WINDOWS_OS_RUNTIME"
    DESCONOCIDO = "DESCONOCIDO"


class EstadoDerechosRedistribucion(StrEnum):
    PROPIO_MIT = "PROPIO_MIT"
    TERCERO_CON_LICENCIA_VERIFICADA = "TERCERO_CON_LICENCIA_VERIFICADA"
    PENDIENTE_REVISION_JURIDICA = "PENDIENTE_REVISION_JURIDICA"
    SYSTEM_LIBRARY_REVISAR = "SYSTEM_LIBRARY_REVISAR"
    NO_VERIFICADO = "NO_VERIFICADO"


@dataclass(frozen=True, slots=True)
class LimitesDeExtraccion:
    """Límites defensivos contra archivos corruptos o bombas de descompresión (SBOM-02)."""

    max_entry_bytes: int = 128 * 1024 * 1024
    max_total_bytes: int = 1024 * 1024 * 1024
    max_depth: int = 4
    max_pyz_toc_bytes: int = 32 * 1024 * 1024


def sha256_archivo(ruta: Path) -> tuple[str, int]:
    """Calcula ``(sha256_hex, bytes)`` en streaming."""
    resumen = hashlib.sha256()
    total = 0
    try:
        with ruta.open("rb") as f:
            while bloque := f.read(_TAMANO_BLOQUE):
                resumen.update(bloque)
                total += len(bloque)
    except OSError as exc:
        raise InventarioError(f"No se pudo leer el ejecutable {ruta}: {exc}") from exc
    return resumen.hexdigest(), total


def _sha256_bytes(datos: bytes) -> str:
    return hashlib.sha256(datos).hexdigest()


def _normalizar_ruta(nombre: str) -> str:
    return nombre.replace("\\", "/").lstrip("./")


def _validar_registro_sha256(registro_ruta: Path, sha_actual: str, bytes_actual: int) -> None:
    try:
        datos = json.loads(registro_ruta.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise InventarioError(f"Registro SHA-256 ilegible ({registro_ruta}): {exc}") from exc
    esperado = datos.get("sha256") if isinstance(datos, dict) else None
    if not isinstance(esperado, str) or _SHA256_RE.fullmatch(esperado) is None:
        raise InventarioError(f"Registro SHA-256 inválido en {registro_ruta}")
    if esperado != sha_actual:
        raise InventarioError(
            f"SHA-256 del ejecutable no coincide con el registro: esperado={esperado} actual={sha_actual}"
        )
    bytes_esp = datos.get("bytes")
    if isinstance(bytes_esp, int) and bytes_esp != bytes_actual:
        raise InventarioError(
            f"Tamaño del ejecutable no coincide con el registro: esperado={bytes_esp} actual={bytes_actual}"
        )


def _descomprimir_acotado(datos_comprimidos: bytes, max_bytes: int, nombre: str) -> bytes:
    """Descomprime flujo zlib con tope estricto de memoria para neutralizar zip-bombs."""
    try:
        decomp = zlib.decompressobj()
        salida = decomp.decompress(datos_comprimidos, max_bytes + 1)
    except zlib.error as exc:
        raise InventarioError(f"Flujo zlib corrupto en '{nombre}': {exc}") from exc
    if len(salida) > max_bytes or decomp.unconsumed_tail:
        raise InventarioError(f"Límite de descompresión excedido en '{nombre}' (máximo {max_bytes} bytes)")
    return salida


def _prevalidar_carchive_crudo(
    exe: Path, tamano_archivo: int
) -> tuple[int, int, list[tuple[str, int, int, int, int, str]]]:
    """Valida cookie, límites de archivo y TOC crudo de CArchive antes de aceptar el lector.

    Detecta duplicados exactos, colisiones case-insensitive (Windows) y offsets/longitudes fuera
    de rango que ``CArchiveReader._parse_toc`` sobrescribiría silenciosamente.
    """
    cookie_fmt = CArchiveReader._COOKIE_FORMAT
    cookie_len = CArchiveReader._COOKIE_LENGTH
    toc_fmt = CArchiveReader._TOC_ENTRY_FORMAT
    toc_len = CArchiveReader._TOC_ENTRY_LENGTH

    with exe.open("rb") as fp:
        cookie_pos = CArchiveReader._find_magic_pattern(fp, CArchiveReader._COOKIE_MAGIC_PATTERN)
        if cookie_pos == -1:
            raise InventarioError("No se encontró la cookie CArchive de PyInstaller (archivo truncado o corrupto)")
        fp.seek(cookie_pos, os.SEEK_SET)
        cookie_raw = fp.read(cookie_len)
        if len(cookie_raw) != cookie_len:
            raise InventarioError("Cookie CArchive truncada")
        _magic, archive_len, toc_offset, toc_length, _pyvers, pylib_raw = struct.unpack(cookie_fmt, cookie_raw)

        end_offset = cookie_pos + cookie_len
        start_offset = end_offset - archive_len
        if archive_len <= cookie_len or start_offset < 0 or end_offset > tamano_archivo:
            raise InventarioError(
                f"Límites de CArchive inválidos: start={start_offset} end={end_offset} file={tamano_archivo}"
            )
        if toc_offset + toc_length > archive_len or toc_length == 0:
            raise InventarioError(
                f"TOC CArchive fuera de rango: offset={toc_offset} len={toc_length} archive={archive_len}"
            )
        if not pylib_raw.rstrip(b"\x00"):
            raise InventarioError("Nombre de librería Python ausente en cookie CArchive")

        fp.seek(start_offset + toc_offset, os.SEEK_SET)
        toc_data = fp.read(toc_length)
        if len(toc_data) != toc_length:
            raise InventarioError("TOC CArchive truncado al leer")

    entradas: list[tuple[str, int, int, int, int, str]] = []
    vistos_ci: dict[str, str] = {}
    pos = 0
    while pos < len(toc_data):
        if pos + toc_len > len(toc_data):
            raise InventarioError("Entrada TOC CArchive truncada")
        entry_len, entry_off, data_len, uncomp_len, comp_flag, tc_raw = struct.unpack(
            toc_fmt, toc_data[pos : pos + toc_len]
        )
        if entry_len <= toc_len or pos + entry_len > len(toc_data):
            raise InventarioError(f"Longitud de entrada TOC inválida en posición {pos}: entry_len={entry_len}")
        name_raw = toc_data[pos + toc_len : pos + entry_len]
        pos += entry_len
        try:
            nombre = name_raw.rstrip(b"\x00").decode("utf-8")
            typecode = tc_raw.decode("ascii")
        except UnicodeDecodeError as exc:
            raise InventarioError(f"Nombre o typecode inválido en TOC CArchive: {exc}") from exc
        if typecode not in _TYPECODES_VALIDOS:
            raise InventarioError(f"Typecode desconocido en TOC CArchive: {typecode!r} para '{nombre}'")
        if typecode != PKG_ITEM_RUNTIME_OPTION:
            if entry_off + data_len > archive_len:
                raise InventarioError(
                    f"Offset/longitud fuera de rango en '{nombre}': offset={entry_off} len={data_len} archive={archive_len}"
                )
            clave_ci = _normalizar_ruta(nombre).casefold()
            if clave_ci in vistos_ci:
                raise InventarioError(
                    f"Entrada duplicada o colisión de nombre en CArchive: '{nombre}' colisiona con '{vistos_ci[clave_ci]}'"
                )
            vistos_ci[clave_ci] = nombre
        entradas.append((nombre, entry_off, data_len, uncomp_len, comp_flag, typecode))

    return start_offset, end_offset, entradas


def _clasificar_entrada_base(nombre_norm: str, typecode: str, contenedor: str) -> tuple[str, str, str]:
    """Clasifica categoría, origen técnico y estado de derechos sin presumir titularidad (SBOM-07)."""
    bajo = nombre_norm.casefold()
    base = Path(nombre_norm).name.casefold()
    ext = Path(nombre_norm).suffix.casefold()

    # Contenedores opacos no inspeccionables (SBOM-01)
    if typecode == PKG_ITEM_ZIPFILE or (ext in _EXTENSIONES_OPACAS and contenedor == "CArchive"):
        return (
            CategoriaComponente.OPAQUE_CONTAINER.value,
            OrigenTecnico.DESCONOCIDO.value,
            EstadoDerechosRedistribucion.NO_VERIFICADO.value,
        )

    # Bootloader / scripts internos de PyInstaller
    if (
        typecode in (PKG_ITEM_PYMODULE, PKG_ITEM_PYPACKAGE)
        and contenedor == "CArchive"
        and (
            base.startswith(("pyi", "_pyi"))
            or base
            in {
                "struct",
                "pyimod01_archive",
                "pyimod02_importers",
                "pyimod03_ctypes",
                "pyimod04_pywin32",
            }
        )
    ) or (typecode == PKG_ITEM_PYSOURCE and base.startswith("pyi")):
        return (
            CategoriaComponente.PYINSTALLER_BOOTLOADER.value,
            OrigenTecnico.PYINSTALLER_BOOTLOADER.value,
            EstadoDerechosRedistribucion.TERCERO_CON_LICENCIA_VERIFICADA.value,
        )

    # SBOM-07: Casos con revisión jurídica pendiente (nunca presumir propiedad exclusiva)
    if bajo in _ACTIVOS_IA_PENDIENTES:
        return (
            CategoriaComponente.FIRST_PARTY_ASSET.value,
            OrigenTecnico.IA_DECLARADA_O_C2PA.value,
            EstadoDerechosRedistribucion.PENDIENTE_REVISION_JURIDICA.value,
        )
    if nombre_norm in _MODULOS_ADAPTACION_UPSTREAM_PENDIENTES or bajo.endswith("auto_parallax_regla.py"):
        return (
            CategoriaComponente.FIRST_PARTY_CODE.value,
            OrigenTecnico.ADAPTACION_UPSTREAM_GPL.value,
            EstadoDerechosRedistribucion.PENDIENTE_REVISION_JURIDICA.value,
        )

    # Fuentes tipográficas SIL OFL-1.1 propias de la GUI
    if bajo.startswith("sky_claw/app/gui/assets/fonts/") and ext in {".woff", ".woff2", ".ttf", ".otf"}:
        return (
            CategoriaComponente.THIRD_PARTY_FONT.value,
            OrigenTecnico.GOOGLE_FONTS_OFL.value,
            EstadoDerechosRedistribucion.TERCERO_CON_LICENCIA_VERIFICADA.value,
        )

    # Código propio y datas propios de sky_claw
    if (
        bajo == "sky_claw"
        or bajo.startswith(("sky_claw/", "sky_claw."))
        or (typecode == PKG_ITEM_PYSOURCE and bajo in {"__main__", "sky_claw/__main__"})
    ):
        if ext in {".css", ".html", ".svg", ".ico"}:
            return (
                CategoriaComponente.FIRST_PARTY_ASSET.value,
                OrigenTecnico.REPO_PROPIO.value,
                EstadoDerechosRedistribucion.PROPIO_MIT.value,
            )
        return (
            CategoriaComponente.FIRST_PARTY_CODE.value,
            OrigenTecnico.REPO_PROPIO.value,
            EstadoDerechosRedistribucion.PROPIO_MIT.value,
        )

    # DLLs de sistema de Windows (SBOM-04 / GPLv3 §1 System Libraries)
    if base in _DLLS_SISTEMA_WINDOWS_EXACTAS or base.startswith(_DLLS_SISTEMA_WINDOWS_PREFIJOS):
        return (
            CategoriaComponente.WINDOWS_SYSTEM_DLL.value,
            OrigenTecnico.WINDOWS_OS_RUNTIME.value,
            EstadoDerechosRedistribucion.SYSTEM_LIBRARY_REVISAR.value,
        )

    # CPython runtime / stdlib embebido (verificado contra sys.stdlib_module_names oficial)
    stem_base = Path(nombre_norm).stem
    raiz_mod = nombre_norm.split(".")[0]
    if (
        contenedor == "ZIP:base_library.zip"
        or re.fullmatch(r"python3\d+\.dll", base)
        or base in _DLLS_CPYTHON_STDLIB
        or (ext == ".pyd" and "/" not in nombre_norm and stem_base in sys.stdlib_module_names)
        or (contenedor.startswith("PYZ:") and raiz_mod in sys.stdlib_module_names)
    ):
        return (
            CategoriaComponente.THIRD_PARTY_NATIVE.value
            if ext in {".dll", ".pyd"}
            else CategoriaComponente.THIRD_PARTY_PYTHON.value,
            OrigenTecnico.CPYTHON_STDLIB.value,
            EstadoDerechosRedistribucion.TERCERO_CON_LICENCIA_VERIFICADA.value,
        )

    # Recursos vendorizados dentro de nicegui/static o elementos frontend
    if bajo.startswith("nicegui/") and ext in {".js", ".css", ".woff", ".woff2", ".ttf", ".svg", ".map", ".html"}:
        cat = (
            CategoriaComponente.THIRD_PARTY_FONT.value
            if ext in {".woff", ".woff2", ".ttf"}
            else CategoriaComponente.THIRD_PARTY_VENDORED.value
        )
        return (cat, OrigenTecnico.PYPI_DISTRIBUCION.value, EstadoDerechosRedistribucion.NO_VERIFICADO.value)

    # Binarios nativos (.pyd/.dll) o módulos PYZ pendientes de atribución por reconciliador
    if ext in {".pyd", ".dll", ".so"} or typecode == PKG_ITEM_BINARY:
        return (
            CategoriaComponente.THIRD_PARTY_NATIVE.value,
            OrigenTecnico.DESCONOCIDO.value,
            EstadoDerechosRedistribucion.NO_VERIFICADO.value,
        )
    if contenedor.startswith("PYZ:"):
        return (
            CategoriaComponente.THIRD_PARTY_PYTHON.value,
            OrigenTecnico.DESCONOCIDO.value,
            EstadoDerechosRedistribucion.NO_VERIFICADO.value,
        )

    return (
        CategoriaComponente.UNIDENTIFIED.value,
        OrigenTecnico.DESCONOCIDO.value,
        EstadoDerechosRedistribucion.NO_VERIFICADO.value,
    )


def _inspeccionar_zip_recursivo(
    datos_zip: bytes,
    nombre_contenedor: str,
    limites: LimitesDeExtraccion,
    profundidad: int,
    total_extraido: list[int],
    entradas_salida: list[dict[str, Any]],
    opacos_salida: list[dict[str, str]],
) -> None:
    """Inspecciona un contenedor ZIP en memoria con límites de profundidad, duplicados y descompresión."""
    if profundidad > limites.max_depth:
        raise InventarioError(
            f"Profundidad máxima de anidamiento ({limites.max_depth}) excedida en '{nombre_contenedor}'"
        )
    try:
        zf = zipfile.ZipFile(io.BytesIO(datos_zip), "r")
    except zipfile.BadZipFile as exc:
        raise InventarioError(f"Contenedor ZIP corrupto '{nombre_contenedor}': {exc}") from exc

    vistos_ci: set[str] = set()
    with zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            norm = _normalizar_ruta(info.filename)
            if ".." in Path(norm).parts:
                raise InventarioError(f"Ruta de traversal detectada en '{nombre_contenedor}': '{info.filename}'")
            clave = norm.casefold()
            if clave in vistos_ci:
                raise InventarioError(f"Entrada duplicada en ZIP '{nombre_contenedor}': '{info.filename}'")
            vistos_ci.add(clave)
            if info.file_size > limites.max_entry_bytes:
                raise InventarioError(f"Límite de descompresión excedido en '{nombre_contenedor}::{norm}'")
            with zf.open(info, "r") as member_fp:
                crudo = member_fp.read(limites.max_entry_bytes + 1)
            if len(crudo) > limites.max_entry_bytes:
                raise InventarioError(f"Límite real de descompresión excedido en '{nombre_contenedor}::{norm}'")
            total_extraido[0] += len(crudo)
            if total_extraido[0] > limites.max_total_bytes:
                raise InventarioError(f"Límite total de descompresión ({limites.max_total_bytes} B) excedido")

            ext = Path(norm).suffix.casefold()
            if ext in _EXTENSIONES_OPACAS:
                opacos_salida.append({"container": nombre_contenedor, "name": norm, "reason": f"extensión opaca {ext}"})
            cat, orig, der = _clasificar_entrada_base(norm, "x", nombre_contenedor)
            entradas_salida.append(
                {
                    "container": nombre_contenedor,
                    "name": norm,
                    "typecode": "zip_member",
                    "compressed_bytes": info.compress_size,
                    "extracted_bytes": len(crudo),
                    "packed_sha256": None,
                    "extracted_sha256": _sha256_bytes(crudo),
                    "component_category": cat,
                    "technical_origin": orig,
                    "redistribution_rights_status": der,
                }
            )
            if ext in _EXTENSIONES_ZIP:
                _inspeccionar_zip_recursivo(
                    crudo,
                    f"{nombre_contenedor}::{norm}",
                    limites,
                    profundidad + 1,
                    total_extraido,
                    entradas_salida,
                    opacos_salida,
                )


def inventariar_ejecutable(
    exe: Path,
    *,
    registro_sha256: Path | None = None,
    limites: LimitesDeExtraccion | None = None,
) -> dict[str, Any]:
    """Extrae el inventario recursivo, determinista y fail-closed de ``exe``."""
    lim = limites or LimitesDeExtraccion()
    if not exe.is_file():
        raise InventarioError(f"El ejecutable no existe o no es un archivo regular: {exe}")

    exe_sha256, exe_bytes = sha256_archivo(exe)
    if registro_sha256 is not None:
        _validar_registro_sha256(registro_sha256, exe_sha256, exe_bytes)

    start_offset, end_offset, toc_crudo = _prevalidar_carchive_crudo(exe, exe_bytes)

    # Hash del bootloader nativo (bytes [0 : start_offset])
    with exe.open("rb") as fp:
        bootloader_bytes = fp.read(start_offset)
    bootloader_sha256 = _sha256_bytes(bootloader_bytes)

    try:
        reader = CArchiveReader(str(exe))
    except (ArchiveReadError, OSError, struct.error, ValueError) as exc:
        raise InventarioError(f"Fallo al abrir CArchiveReader sobre {exe}: {exc}") from exc

    entradas: list[dict[str, Any]] = []
    opacos: list[dict[str, str]] = []
    opciones_runtime: list[str] = []
    total_extraido = [0]

    with exe.open("rb") as fp:
        for nombre, entry_off, data_len, uncomp_len, comp_flag, typecode in toc_crudo:
            if typecode == PKG_ITEM_RUNTIME_OPTION:
                opciones_runtime.append(nombre)
                continue

            if uncomp_len > lim.max_entry_bytes:
                raise InventarioError(
                    f"Límite de descompresión excedido en '{nombre}': declarado={uncomp_len} max={lim.max_entry_bytes}"
                )

            fp.seek(start_offset + entry_off, os.SEEK_SET)
            packed_raw = fp.read(data_len)
            if len(packed_raw) != data_len:
                raise InventarioError(f"Entrada truncada en CArchive: '{nombre}'")
            packed_sha = _sha256_bytes(packed_raw)

            extracted_raw = _descomprimir_acotado(packed_raw, lim.max_entry_bytes, nombre) if comp_flag else packed_raw

            total_extraido[0] += len(extracted_raw)
            if total_extraido[0] > lim.max_total_bytes:
                raise InventarioError(f"Límite total de descompresión ({lim.max_total_bytes} B) excedido en '{nombre}'")

            norm = _normalizar_ruta(nombre)
            ext = Path(norm).suffix.casefold()
            cat, orig, der = _clasificar_entrada_base(norm, typecode, "CArchive")
            if cat == CategoriaComponente.OPAQUE_CONTAINER.value:
                opacos.append({"container": "CArchive", "name": norm, "reason": f"contenedor opaco ({typecode}/{ext})"})

            entradas.append(
                {
                    "container": "CArchive",
                    "name": norm,
                    "typecode": typecode,
                    "compressed_bytes": data_len,
                    "extracted_bytes": len(extracted_raw),
                    "packed_sha256": packed_sha,
                    "extracted_sha256": _sha256_bytes(extracted_raw),
                    "component_category": cat,
                    "technical_origin": orig,
                    "redistribution_rights_status": der,
                }
            )

            # Inspección recursiva de PYZ embebido (SBOM-01 + SBOM-02)
            if typecode == PKG_ITEM_PYZ:
                if data_len > lim.max_entry_bytes:
                    raise InventarioError(f"PYZ '{norm}' excede límite de tamaño")
                try:
                    pyz = reader.open_embedded_archive(nombre)
                except Exception as exc:
                    raise InventarioError(f"No se pudo abrir PYZ embebido '{norm}': {exc}") from exc
                pyz_cont = f"PYZ:{norm}"
                for mod_nombre, (ispkg, mod_pos, mod_len) in sorted(pyz.toc.items()):
                    if mod_len > lim.max_entry_bytes:
                        raise InventarioError(f"Módulo PYZ '{mod_nombre}' excede límite de descompresión")
                    fp.seek(start_offset + entry_off + mod_pos, os.SEEK_SET)
                    mod_packed = fp.read(mod_len)
                    if len(mod_packed) != mod_len:
                        raise InventarioError(f"Módulo PYZ truncado: '{mod_nombre}'")
                    mod_extracted = _descomprimir_acotado(mod_packed, lim.max_entry_bytes, mod_nombre)
                    total_extraido[0] += len(mod_extracted)
                    if total_extraido[0] > lim.max_total_bytes:
                        raise InventarioError(f"Límite total de descompresión excedido en PYZ '{mod_nombre}'")
                    m_cat, m_orig, m_der = _clasificar_entrada_base(mod_nombre, "pyz_module", pyz_cont)
                    entradas.append(
                        {
                            "container": pyz_cont,
                            "name": mod_nombre,
                            "typecode": "pyz_pkg" if ispkg else "pyz_mod",
                            "compressed_bytes": mod_len,
                            "extracted_bytes": len(mod_extracted),
                            "packed_sha256": _sha256_bytes(mod_packed),
                            "extracted_sha256": _sha256_bytes(mod_extracted),
                            "component_category": m_cat,
                            "technical_origin": m_orig,
                            "redistribution_rights_status": m_der,
                        }
                    )

            # Inspección recursiva de base_library.zip y otros ZIP/WHL/EGG anidados
            elif ext in _EXTENSIONES_ZIP:
                _inspeccionar_zip_recursivo(
                    extracted_raw,
                    f"ZIP:{norm}",
                    lim,
                    profundidad=1,
                    total_extraido=total_extraido,
                    entradas_salida=entradas,
                    opacos_salida=opacos,
                )

    entradas.sort(key=lambda item: (str(item["container"]), str(item["name"]), str(item["typecode"])))
    opacos.sort(key=lambda item: (item["container"], item["name"]))
    opciones_runtime.sort()

    return {
        "schema_version": 1,
        "exe": {
            "name": exe.name,
            "bytes": exe_bytes,
            "sha256": exe_sha256,
        },
        "bootloader": {
            "bytes": len(bootloader_bytes),
            "sha256": bootloader_sha256,
            "carchive_start_offset": start_offset,
            "carchive_end_offset": end_offset,
        },
        "runtime_options": opciones_runtime,
        "opaque_containers": opacos,
        "total_extracted_bytes": total_extraido[0],
        "entry_count": len(entradas),
        "entries": entradas,
    }


def escribir_inventario(inventario: dict[str, Any], destino: Path) -> None:
    """Persiste ``inventory.json`` de forma determinista y fail-closed."""
    try:
        if destino.is_dir():
            raise OSError(f"El destino es un directorio: {destino}")
        destino.parent.mkdir(parents=True, exist_ok=True)
        destino.write_text(
            json.dumps(inventario, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    except OSError as exc:
        raise InventarioError(f"Error al escribir el inventario en {destino}: {exc}") from exc


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Extrae el inventario efectivo y determinista de SkyClawApp.exe.")
    parser.add_argument("exe", type=Path, help="Ruta al ejecutable congelado (PyInstaller).")
    parser.add_argument("--out", type=Path, required=True, help="Ruta de salida para inventory.json.")
    parser.add_argument(
        "--registro-sha256",
        type=Path,
        default=None,
        help="JSON generado por scripts/registrar_sha256_ejecutable.py para verificar identidad del EXE.",
    )
    args = parser.parse_args(argv)
    try:
        inventario = inventariar_ejecutable(args.exe, registro_sha256=args.registro_sha256)
        escribir_inventario(inventario, args.out)
    except InventarioError as exc:
        print(f"inventario_ejecutable: {exc}", file=sys.stderr)
        return EXIT_VALIDACION
    print(f"inventario_ejecutable: OK entries={inventario['entry_count']} sha256={inventario['exe']['sha256']}")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())

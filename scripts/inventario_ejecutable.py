"""Inventario determinista, recursivo y fail-closed de un ejecutable PyInstaller (PR-LIC-03A).

Inspecciona en modo estrictamente pasivo (sin ejecutar el EXE, sin importar módulos extraídos
y sin requerir ``PyInstaller`` instalado en el entorno de inspección):
- Segmento de bootloader nativo de PyInstaller (bytes anteriores a ``CArchive``).
- ``CArchive`` (validación directa de cookie, offsets, límites de descompresión, tamaño declarado
  vs extraído, duplicados exactos y colisiones insensibles a mayúsculas sobre un único descriptor
  de archivo con re-verificación de SHA-256 anti-TOCTOU).
- ``PYZ`` embebido (validación de cabecera ``PYZ\\x00``, ``toc_offset``, cota ``max_pyz_toc_bytes``
  previa a ``marshal.loads``, duplicados antes de dict y rangos internos de cada módulo).
- ``base_library.zip`` y contenedores ``.zip``/``.whl``/``.egg`` anidados hasta ``max_depth``.
- Detección explícita de contenedores opacos no inspeccionables (``.7z``, ``.tar``, ``.rar``,
  ``.cab``, ``PKG_ITEM_ZIPFILE``) para fallo cerrado en reconciliación (SBOM-01).

Distingue siempre ``packed_sha256`` (bytes crudos/comprimidos en el contenedor) y
``extracted_sha256`` (bytes descomprimidos) conforme a SBOM-04, y separa categoría técnica,
origen técnico (incluyendo OpenSSL, SQLite, libffi, Tcl/Tk, CPython y Microsoft runtime) y estado
de derechos de redistribución conforme a SBOM-07 y R1 (F03/F06).
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import marshal
import os
import re
import struct
import sys
import tempfile
import zipfile
import zlib
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, BinaryIO

EXIT_OK = 0
EXIT_VALIDACION = 1
EXIT_ERROR = 2

_TAMANO_BLOQUE = 1024 * 1024
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

# Constantes del formato binario de PyInstaller CArchive / PYZ (sin depender de PyInstaller en runtime)
PKG_ITEM_BINARY = "b"
PKG_ITEM_DATA = "x"
PKG_ITEM_DEPENDENCY = "d"
PKG_ITEM_PYMODULE = "m"
PKG_ITEM_PYPACKAGE = "M"
PKG_ITEM_PYSOURCE = "s"
PKG_ITEM_PYZ = "z"
PKG_ITEM_RUNTIME_OPTION = "o"
PKG_ITEM_SPLASH = "l"
PKG_ITEM_ZIPFILE = "Z"

_COOKIE_MAGIC = b"MEI\014\013\012\013\016"
_COOKIE_FORMAT = "!8sIIII64s"
_COOKIE_LENGTH = struct.calcsize(_COOKIE_FORMAT)
_TOC_ENTRY_FORMAT = "!IIIIBc"
_TOC_ENTRY_LENGTH = struct.calcsize(_TOC_ENTRY_FORMAT)
_PYZ_MAGIC = b"PYZ\x00"
_PYZ_HEADER_LENGTH = 16

#: Prefijos exactos de ``datas`` declarados en ``sky_claw.spec`` (alineado con origin/main).
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

#: Allowlist estricta de módulos bootstrap propios de PyInstaller (F06: evita prefijos genéricos ``pyi*``).
_MODULOS_BOOTLOADER_PYINSTALLER = frozenset(
    {
        "struct",
        "pyimod01_archive",
        "pyimod02_importers",
        "pyimod03_ctypes",
        "pyimod04_pywin32",
        "pyiboot01_bootstrap",
        "_pyi_rth_utils",
    }
)

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
_DLLS_OPENSSL = frozenset(
    {
        "libcrypto-3.dll",
        "libcrypto-3-x64.dll",
        "libcrypto-1_1.dll",
        "libcrypto-1_1-x64.dll",
        "libssl-3.dll",
        "libssl-3-x64.dll",
        "libssl-1_1.dll",
        "libssl-1_1-x64.dll",
    }
)
_DLLS_SQLITE = frozenset({"sqlite3.dll"})
_DLLS_LIBFFI = frozenset({"libffi-8.dll", "libffi-7.dll", "libffi-6.dll"})
_DLLS_TCL_TK = frozenset({"tcl86t.dll", "tk86t.dll", "tcl86.dll", "tk86.dll"})
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
    MO2_PLUGIN_IN_PROCESS = "MO2_PLUGIN_IN_PROCESS"
    IA_DECLARADA_O_C2PA = "IA_DECLARADA_O_C2PA"
    ADAPTACION_UPSTREAM_GPL = "ADAPTACION_UPSTREAM_GPL"
    GOOGLE_FONTS_OFL = "GOOGLE_FONTS_OFL"
    PYPI_DISTRIBUCION = "PYPI_DISTRIBUCION"
    CPYTHON_STDLIB = "CPYTHON_STDLIB"
    OPENSSL_EMBEDDED = "OPENSSL_EMBEDDED"
    SQLITE_EMBEDDED = "SQLITE_EMBEDDED"
    LIBFFI_EMBEDDED = "LIBFFI_EMBEDDED"
    TCL_TK_EMBEDDED = "TCL_TK_EMBEDDED"
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
    """Límites defensivos contra archivos corruptos, bombas de descompresión o DoS (SBOM-02/F06)."""

    max_entry_bytes: int = 128 * 1024 * 1024
    max_total_bytes: int = 1024 * 1024 * 1024
    max_depth: int = 4
    max_pyz_toc_bytes: int = 32 * 1024 * 1024
    max_entry_count: int = 50_000


def _sha256_fp(fp: BinaryIO) -> tuple[str, int]:
    """Calcula ``(sha256_hex, bytes)`` leyendo un descriptor ya abierto desde el inicio."""
    fp.seek(0, os.SEEK_SET)
    resumen = hashlib.sha256()
    total = 0
    while bloque := fp.read(_TAMANO_BLOQUE):
        resumen.update(bloque)
        total += len(bloque)
    return resumen.hexdigest(), total


def sha256_archivo(ruta: Path) -> tuple[str, int]:
    """Calcula ``(sha256_hex, bytes)`` en streaming."""
    try:
        with ruta.open("rb") as fp:
            return _sha256_fp(fp)
    except OSError as exc:
        raise InventarioError(f"No se pudo leer el ejecutable {ruta}: {exc}") from exc


def _sha256_bytes(datos: bytes) -> str:
    return hashlib.sha256(datos).hexdigest()


_UNIDAD_WINDOWS_RE = re.compile(r"^[A-Za-z]:")


def _validar_y_normalizar_ruta(nombre_crudo: str, contexto: str) -> str:
    """Valida el nombre crudo ANTES de normalizar para impedir que la normalización oculte traversal/absolutas (F08)."""
    if not nombre_crudo or "\x00" in nombre_crudo:
        raise InventarioError(f"Ruta vacía o con byte NUL en {contexto}: {nombre_crudo!r}")
    if nombre_crudo.startswith(("\\\\", "//")):
        raise InventarioError(f"Ruta UNC prohibida en {contexto}: {nombre_crudo!r}")
    if _UNIDAD_WINDOWS_RE.match(nombre_crudo):
        raise InventarioError(f"Ruta Windows con unidad prohibida en {contexto}: {nombre_crudo!r}")
    if nombre_crudo.startswith(("/", "\\")):
        raise InventarioError(f"Ruta absoluta prohibida en {contexto}: {nombre_crudo!r}")

    segmentos_crudos = re.split(r"[/\\]", nombre_crudo)
    if ".." in segmentos_crudos:
        raise InventarioError(f"Ruta de traversal ('..') prohibida en {contexto}: {nombre_crudo!r}")
    if any(seg == "" for seg in segmentos_crudos):
        raise InventarioError(f"Ruta con separadores vacíos o redundantes en {contexto}: {nombre_crudo!r}")

    segmentos_limpios = [seg for seg in segmentos_crudos if seg != "."]
    if not segmentos_limpios:
        raise InventarioError(f"Ruta inválida sin segmentos efectivos en {contexto}: {nombre_crudo!r}")
    return "/".join(segmentos_limpios)


def _validar_nombre_modulo_pyz(mod_nombre: str, nombre_pyz: str) -> str:
    """Valida que el nombre de módulo en PYZ no contenga traversal ni separadores de ruta (F08)."""
    if not mod_nombre or "\x00" in mod_nombre:
        raise InventarioError(f"Nombre de módulo PYZ vacío o con NUL en '{nombre_pyz}'")
    if any(c in mod_nombre for c in ("/", "\\", ":")) or ".." in mod_nombre:
        raise InventarioError(f"Nombre de módulo PYZ inválido o con traversal en '{nombre_pyz}': {mod_nombre!r}")
    return mod_nombre


def _normalizar_ruta(nombre: str) -> str:
    """Alias seguro que valida antes de normalizar separadores (F08)."""
    return _validar_y_normalizar_ruta(nombre, "entrada")


def _formatear_pyvers_cookie(pyvers: int) -> str:
    """Convierte el entero ``pyvers`` de la cookie PyInstaller (p. ej. 311) en ``'3.11'``."""
    if pyvers >= 100:
        return f"{pyvers // 100}.{pyvers % 100}"
    if pyvers >= 10:
        return f"{pyvers // 10}.{pyvers % 10}"
    return ""


def _extraer_version_pe_cpython(datos_dll: bytes) -> str:
    """Extrae la versión exacta ``major.minor.micro`` desde el directorio de recursos PE real via ``pefile`` (F15).

    No busca firmas crudas con ``bytes.find()`` para evitar falsos positivos fuera del directorio
    de recursos PE. Si ``pefile`` no está disponible, el binario no es un PE válido o carece de
    ``VS_FIXEDFILEINFO`` real, devuelve ``""`` (fail-closed → ``NOASSERTION`` en reconciliación).
    """
    try:
        import pefile
    except ImportError:
        return ""

    pe: Any = None
    try:
        pe = pefile.PE(data=datos_dll, fast_load=True)
        pe.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_RESOURCE"]])
        fixed_list = getattr(pe, "VS_FIXEDFILEINFO", None)
        if not fixed_list:
            return ""
        fi = fixed_list[0]
        if int(getattr(fi, "Signature", 0)) != 0xFEEF04BD:
            return ""
        ms = int(fi.FileVersionMS)
        ls = int(fi.FileVersionLS)
    except Exception:  # noqa: BLE001 — pefile puede lanzar PEFormatError/struct.error sobre binarios no-PE
        return ""
    finally:
        if pe is not None:
            pe.close()

    major = ms >> 16
    minor = ms & 0xFFFF
    field3 = ls >> 16
    if major != 3 or minor > 99:
        return ""
    micro = field3 // 1000 if field3 >= 1000 else field3
    if micro > 99:
        return ""
    return f"{major}.{minor}.{micro}"


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
    """Descomprime flujo zlib con tope estricto, exigiendo EOF y ausencia de bytes sobrantes (F06)."""
    try:
        decomp = zlib.decompressobj()
        salida = decomp.decompress(datos_comprimidos, max_bytes + 1)
        if len(salida) > max_bytes or decomp.unconsumed_tail:
            raise InventarioError(f"Límite de descompresión excedido en '{nombre}' (máximo {max_bytes} bytes)")
        cola = decomp.flush(max_bytes - len(salida) + 1)
        salida += cola
    except zlib.error as exc:
        raise InventarioError(f"Flujo zlib corrupto en '{nombre}': {exc}") from exc
    if len(salida) > max_bytes:
        raise InventarioError(f"Límite de descompresión excedido en '{nombre}' (máximo {max_bytes} bytes)")
    if not decomp.eof:
        raise InventarioError(f"Flujo zlib incompleto/truncado (sin EOF) en '{nombre}'")
    if decomp.unused_data:
        raise InventarioError(f"Bytes sobrantes tras el fin del flujo zlib en '{nombre}' ({len(decomp.unused_data)} B)")
    return salida


def _buscar_cookie_carchive(fp: BinaryIO, tamano_archivo: int) -> int:
    """Localiza la última ocurrencia de ``_COOKIE_MAGIC`` escaneando hacia atrás en bloques."""
    bloque = 64 * 1024
    solapamiento = len(_COOKIE_MAGIC)
    pos_fin = tamano_archivo
    while pos_fin > 0:
        pos_ini = max(0, pos_fin - bloque)
        longitud = min(tamano_archivo - pos_ini, (pos_fin - pos_ini) + solapamiento)
        fp.seek(pos_ini, os.SEEK_SET)
        datos = fp.read(longitud)
        idx = datos.rfind(_COOKIE_MAGIC)
        if idx != -1:
            return pos_ini + idx
        pos_fin = pos_ini
    return -1


def _prevalidar_carchive_crudo(
    fp: BinaryIO, tamano_archivo: int
) -> tuple[int, int, str, str, list[tuple[str, int, int, int, int, str]]]:
    """Valida cookie, límites de archivo y TOC crudo de CArchive sobre un descriptor ya abierto.

    Detecta duplicados exactos, colisiones case-insensitive (Windows) y offsets/longitudes fuera
    de rango.
    """
    cookie_pos = _buscar_cookie_carchive(fp, tamano_archivo)
    if cookie_pos == -1:
        raise InventarioError("No se encontró la cookie CArchive de PyInstaller (archivo truncado o corrupto)")
    fp.seek(cookie_pos, os.SEEK_SET)
    cookie_raw = fp.read(_COOKIE_LENGTH)
    if len(cookie_raw) != _COOKIE_LENGTH:
        raise InventarioError("Cookie CArchive truncada")
    _magic, archive_len, toc_offset, toc_length, pyvers, pylib_raw = struct.unpack(_COOKIE_FORMAT, cookie_raw)

    end_offset = cookie_pos + _COOKIE_LENGTH
    start_offset = end_offset - archive_len
    if archive_len <= _COOKIE_LENGTH or start_offset < 0 or end_offset > tamano_archivo:
        raise InventarioError(
            f"Límites de CArchive inválidos: start={start_offset} end={end_offset} file={tamano_archivo}"
        )
    if toc_offset + toc_length > archive_len or toc_length == 0:
        raise InventarioError(
            f"TOC CArchive fuera de rango: offset={toc_offset} len={toc_length} archive={archive_len}"
        )
    pylib_bytes = pylib_raw.rstrip(b"\x00")
    if not pylib_bytes:
        raise InventarioError("Nombre de librería Python ausente en cookie CArchive")
    try:
        pylib_name = pylib_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise InventarioError(f"Nombre de librería Python inválido en cookie CArchive: {exc}") from exc

    fp.seek(start_offset + toc_offset, os.SEEK_SET)
    toc_data = fp.read(toc_length)
    if len(toc_data) != toc_length:
        raise InventarioError("TOC CArchive truncado al leer")

    entradas: list[tuple[str, int, int, int, int, str]] = []
    vistos_ci: dict[str, str] = {}
    pos = 0
    while pos < len(toc_data):
        if pos + _TOC_ENTRY_LENGTH > len(toc_data):
            raise InventarioError("Entrada TOC CArchive truncada")
        entry_len, entry_off, data_len, uncomp_len, comp_flag, tc_raw = struct.unpack(
            _TOC_ENTRY_FORMAT, toc_data[pos : pos + _TOC_ENTRY_LENGTH]
        )
        if entry_len <= _TOC_ENTRY_LENGTH or pos + entry_len > len(toc_data):
            raise InventarioError(f"Longitud de entrada TOC inválida en posición {pos}: entry_len={entry_len}")
        name_raw = toc_data[pos + _TOC_ENTRY_LENGTH : pos + entry_len]
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
            nombre_norm = _validar_y_normalizar_ruta(nombre, "CArchive")
            clave_ci = nombre_norm.casefold()
            if clave_ci in vistos_ci:
                raise InventarioError(
                    f"Entrada duplicada o colisión de nombre en CArchive: '{nombre}' colisiona con '{vistos_ci[clave_ci]}'"
                )
            vistos_ci[clave_ci] = nombre
        entradas.append((nombre, entry_off, data_len, uncomp_len, comp_flag, typecode))

    return start_offset, end_offset, _formatear_pyvers_cookie(pyvers), pylib_name, entradas


def _parsear_pyz_seguro(
    pyz_bytes: bytes,
    nombre_pyz: str,
    limites: LimitesDeExtraccion,
) -> list[tuple[str, int, int, int]]:
    """Valida cabecera PYZ, aplica ``max_pyz_toc_bytes`` previo a ``marshal.loads``,
    detecta duplicados antes de dict y valida rangos internos (F06).
    """
    if len(pyz_bytes) < _PYZ_HEADER_LENGTH or pyz_bytes[:4] != _PYZ_MAGIC:
        raise InventarioError(f"Cabecera PYZ inválida o truncada en '{nombre_pyz}'")
    (toc_offset,) = struct.unpack("!I", pyz_bytes[8:12])
    if toc_offset < _PYZ_HEADER_LENGTH or toc_offset >= len(pyz_bytes):
        raise InventarioError(f"TOC offset fuera de rango en PYZ '{nombre_pyz}': {toc_offset}")
    toc_size = len(pyz_bytes) - toc_offset
    if toc_size > limites.max_pyz_toc_bytes:
        raise InventarioError(
            f"TOC de PYZ '{nombre_pyz}' excede límite max_pyz_toc_bytes ({toc_size} > {limites.max_pyz_toc_bytes})"
        )

    try:
        # F18 (modelo de confianza): el TOC PYZ de PyInstaller usa formato marshal por especificación;
        # la cota max_pyz_toc_bytes mitiga agotamiento de memoria, pero marshal.loads NO es seguro
        # frente a binarios arbitrarios no confiables. Esta herramienta está certificada solo para
        # builds propios controlados y exige aislamiento de SO para artefactos no confiables.
        toc_obj = marshal.loads(pyz_bytes[toc_offset:])  # nosec B302
    except (ValueError, EOFError, TypeError) as exc:
        raise InventarioError(f"TOC marshal corrupto en PYZ '{nombre_pyz}': {exc}") from exc

    pares: list[tuple[Any, Any]]
    if isinstance(toc_obj, list):
        pares = [(item[0], item[1]) for item in toc_obj if isinstance(item, tuple) and len(item) == 2]
        if len(pares) != len(toc_obj):
            raise InventarioError(f"Estructura de entrada inválida en TOC de PYZ '{nombre_pyz}'")
    elif isinstance(toc_obj, dict):
        pares = list(toc_obj.items())
    else:
        raise InventarioError(f"TOC de PYZ '{nombre_pyz}' tiene tipo no soportado: {type(toc_obj).__name__}")

    resultado: list[tuple[str, int, int, int]] = []
    vistos_ci: dict[str, str] = {}
    for mod_raw, meta in pares:
        if isinstance(mod_raw, bytes):
            try:
                mod_nombre = mod_raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise InventarioError(f"Nombre de módulo PYZ inválido en '{nombre_pyz}': {exc}") from exc
        elif isinstance(mod_raw, str):
            mod_nombre = mod_raw
        else:
            raise InventarioError(f"Nombre de módulo no textual en PYZ '{nombre_pyz}'")
        mod_nombre = _validar_nombre_modulo_pyz(mod_nombre, nombre_pyz)

        if not isinstance(meta, tuple) or len(meta) != 3:
            raise InventarioError(f"Metadatos inválidos para módulo PYZ '{mod_nombre}'")
        ispkg, mod_pos, mod_len = meta
        if not all(isinstance(v, int) for v in (ispkg, mod_pos, mod_len)):
            raise InventarioError(f"Offsets/tipos no enteros en módulo PYZ '{mod_nombre}'")
        if mod_pos < _PYZ_HEADER_LENGTH or mod_len < 0 or mod_pos + mod_len > toc_offset:
            raise InventarioError(
                f"Offset/longitud fuera de rango en módulo PYZ '{mod_nombre}': pos={mod_pos} len={mod_len} toc={toc_offset}"
            )
        clave_ci = mod_nombre.casefold()
        if clave_ci in vistos_ci:
            raise InventarioError(
                f"Entrada duplicada o colisión en PYZ '{nombre_pyz}': '{mod_nombre}' colisiona con '{vistos_ci[clave_ci]}'"
            )
        vistos_ci[clave_ci] = mod_nombre
        resultado.append((mod_nombre, ispkg, mod_pos, mod_len))

    resultado.sort(key=lambda t: t[0])
    return resultado


def _clasificar_entrada_base(nombre_norm: str, typecode: str, contenedor: str) -> tuple[str, str, str]:
    """Clasifica categoría, origen técnico y estado de derechos sin presumir titularidad (SBOM-07/F03/F06)."""
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

    # Bootloader / scripts internos de PyInstaller (allowlist estricta, F06)
    es_hook_pyi = base.startswith(("pyi_rth_", "_pyi_rth_"))
    if (
        typecode in (PKG_ITEM_PYMODULE, PKG_ITEM_PYPACKAGE)
        and contenedor == "CArchive"
        and (base in _MODULOS_BOOTLOADER_PYINSTALLER or es_hook_pyi)
    ) or (typecode == PKG_ITEM_PYSOURCE and (base in _MODULOS_BOOTLOADER_PYINSTALLER or es_hook_pyi)):
        return (
            CategoriaComponente.PYINSTALLER_BOOTLOADER.value,
            OrigenTecnico.PYINSTALLER_BOOTLOADER.value,
            EstadoDerechosRedistribucion.TERCERO_CON_LICENCIA_VERIFICADA.value,
        )

    # F16: las clasificaciones por prefijo/nombre exigen contenedor autorizado (nunca un ZIP anidado arbitrario)
    es_carchive = contenedor == "CArchive"
    es_pyz = contenedor.startswith("PYZ:")
    es_zip_anidado = contenedor.startswith("ZIP:")

    # SBOM-07: Casos con revisión jurídica pendiente (nunca presumir propiedad exclusiva)
    if es_carchive and bajo in _ACTIVOS_IA_PENDIENTES:
        return (
            CategoriaComponente.FIRST_PARTY_ASSET.value,
            OrigenTecnico.IA_DECLARADA_O_C2PA.value,
            EstadoDerechosRedistribucion.PENDIENTE_REVISION_JURIDICA.value,
        )
    if (es_carchive or es_pyz) and (
        nombre_norm in _MODULOS_ADAPTACION_UPSTREAM_PENDIENTES or bajo.endswith("auto_parallax_regla.py")
    ):
        return (
            CategoriaComponente.FIRST_PARTY_CODE.value,
            OrigenTecnico.ADAPTACION_UPSTREAM_GPL.value,
            EstadoDerechosRedistribucion.PENDIENTE_REVISION_JURIDICA.value,
        )

    # F09: Plugin skyclaw_bridge (frontera in-process con Mod Organizer 2, revisión jurídica pendiente)
    if (es_carchive or es_pyz) and (
        bajo.startswith("sky_claw/local/mo2/plugin_bundle/skyclaw_bridge/")
        or bajo == "sky_claw/local/mo2/plugin_bundle/skyclaw_bridge"
        or bajo.startswith("sky_claw.local.mo2.plugin_bundle.skyclaw_bridge")
    ):
        return (
            CategoriaComponente.FIRST_PARTY_CODE.value,
            OrigenTecnico.MO2_PLUGIN_IN_PROCESS.value,
            EstadoDerechosRedistribucion.PENDIENTE_REVISION_JURIDICA.value,
        )

    # Fuentes tipográficas SIL OFL-1.1 propias de la GUI (solo en CArchive)
    if es_carchive and bajo.startswith("sky_claw/app/gui/assets/fonts/") and ext in {".woff", ".woff2", ".ttf", ".otf"}:
        return (
            CategoriaComponente.THIRD_PARTY_FONT.value,
            OrigenTecnico.GOOGLE_FONTS_OFL.value,
            EstadoDerechosRedistribucion.TERCERO_CON_LICENCIA_VERIFICADA.value,
        )

    # Código propio y datas propios de sky_claw (solo en CArchive o PYZ principal; nunca en ZIP anidado, F16)
    if (es_carchive or es_pyz) and (
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

    # DLLs de sistema de Windows (solo en CArchive)
    if es_carchive and (base in _DLLS_SISTEMA_WINDOWS_EXACTAS or base.startswith(_DLLS_SISTEMA_WINDOWS_PREFIJOS)):
        return (
            CategoriaComponente.WINDOWS_SYSTEM_DLL.value,
            OrigenTecnico.WINDOWS_OS_RUNTIME.value,
            EstadoDerechosRedistribucion.SYSTEM_LIBRARY_REVISAR.value,
        )

    # F03: Bibliotecas nativas de terceros embebidas por CPython Windows (solo en CArchive)
    if es_carchive and base in _DLLS_OPENSSL:
        return (
            CategoriaComponente.THIRD_PARTY_NATIVE.value,
            OrigenTecnico.OPENSSL_EMBEDDED.value,
            EstadoDerechosRedistribucion.NO_VERIFICADO.value,
        )
    if es_carchive and base in _DLLS_SQLITE:
        return (
            CategoriaComponente.THIRD_PARTY_NATIVE.value,
            OrigenTecnico.SQLITE_EMBEDDED.value,
            EstadoDerechosRedistribucion.NO_VERIFICADO.value,
        )
    if es_carchive and base in _DLLS_LIBFFI:
        return (
            CategoriaComponente.THIRD_PARTY_NATIVE.value,
            OrigenTecnico.LIBFFI_EMBEDDED.value,
            EstadoDerechosRedistribucion.NO_VERIFICADO.value,
        )
    if es_carchive and (base in _DLLS_TCL_TK or bajo.startswith(("_tcl_data/", "_tk_data/"))):
        return (
            CategoriaComponente.THIRD_PARTY_NATIVE.value,
            OrigenTecnico.TCL_TK_EMBEDDED.value,
            EstadoDerechosRedistribucion.NO_VERIFICADO.value,
        )

    # CPython runtime / stdlib embebido (verificado contra sys.stdlib_module_names oficial; en base_library.zip exige módulo stdlib, F16)
    stem_base = Path(nombre_norm).stem
    raiz_mod = nombre_norm.split(".")[0]
    raiz_ruta = nombre_norm.split("/")[0]
    raiz_zip_mod = Path(raiz_ruta).stem if "/" not in nombre_norm else raiz_ruta
    if (
        (contenedor == "ZIP:base_library.zip" and raiz_zip_mod in sys.stdlib_module_names)
        or (es_carchive and (base == "python3.dll" or bool(re.fullmatch(r"python3\d+\.dll", base))))
        or (es_carchive and ext == ".pyd" and "/" not in nombre_norm and stem_base in sys.stdlib_module_names)
        or (es_pyz and raiz_mod in sys.stdlib_module_names)
    ):
        return (
            CategoriaComponente.THIRD_PARTY_NATIVE.value
            if ext in {".dll", ".pyd"}
            else CategoriaComponente.THIRD_PARTY_PYTHON.value,
            OrigenTecnico.CPYTHON_STDLIB.value,
            EstadoDerechosRedistribucion.TERCERO_CON_LICENCIA_VERIFICADA.value,
        )

    # Recursos vendorizados dentro de nicegui/static o elementos frontend (solo en CArchive, F03/F16)
    if (
        es_carchive
        and bajo.startswith("nicegui/")
        and ext in {".js", ".css", ".woff", ".woff2", ".ttf", ".svg", ".map", ".html"}
    ):
        cat = (
            CategoriaComponente.THIRD_PARTY_FONT.value
            if ext in {".woff", ".woff2", ".ttf"}
            else CategoriaComponente.THIRD_PARTY_VENDORED.value
        )
        return (cat, OrigenTecnico.PYPI_DISTRIBUCION.value, EstadoDerechosRedistribucion.NO_VERIFICADO.value)

    # Cualquier miembro no stdlib dentro de un ZIP anidado permanece UNIDENTIFIED para fallo cerrado (F16)
    if es_zip_anidado:
        return (
            CategoriaComponente.UNIDENTIFIED.value,
            OrigenTecnico.DESCONOCIDO.value,
            EstadoDerechosRedistribucion.NO_VERIFICADO.value,
        )

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


def _verificar_tope_entradas(entradas: list[dict[str, Any]], limites: LimitesDeExtraccion) -> None:
    if len(entradas) >= limites.max_entry_count:
        raise InventarioError(f"Límite máximo de entradas (max_entry_count={limites.max_entry_count}) excedido")


def _inspeccionar_zip_recursivo(
    datos_zip: bytes,
    nombre_contenedor: str,
    limites: LimitesDeExtraccion,
    profundidad: int,
    total_extraido: list[int],
    entradas_salida: list[dict[str, Any]],
    opacos_salida: list[dict[str, str]],
) -> None:
    """Inspecciona un contenedor ZIP en memoria con límites de profundidad, cantidad, duplicados y descompresión."""
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
            _verificar_tope_entradas(entradas_salida, limites)
            norm = _validar_y_normalizar_ruta(info.filename, nombre_contenedor)
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
            if len(crudo) != info.file_size:
                raise InventarioError(
                    f"Tamaño declarado ({info.file_size}) distinto del extraído ({len(crudo)}) en '{nombre_contenedor}::{norm}'"
                )
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


def _crear_copia_aislada_verificada(exe: Path) -> tuple[Path, str, int]:
    """Copia ``exe`` en streaming a un snapshot temporal aislado y re-verifica el origen en un descriptor fresco (F17)."""
    resumen = hashlib.sha256()
    total = 0
    fd, tmp_name = tempfile.mkstemp(prefix="skyclaw_inv_snap_", suffix=".bin")
    snap_path = Path(tmp_name)
    try:
        with exe.open("rb") as src, os.fdopen(fd, "wb") as dst:
            while bloque := src.read(_TAMANO_BLOQUE):
                resumen.update(bloque)
                dst.write(bloque)
                total += len(bloque)
        sha_copia = resumen.hexdigest()
        sha_origen_post, bytes_origen_post = sha256_archivo(exe)
        if sha_origen_post != sha_copia or bytes_origen_post != total:
            raise InventarioError("El ejecutable cambió durante la captura de copia aislada (detección anti-TOCTOU)")
        return snap_path, sha_copia, total
    except Exception:
        snap_path.unlink(missing_ok=True)
        raise


def inventariar_ejecutable(
    exe: Path,
    *,
    registro_sha256: Path | None = None,
    limites: LimitesDeExtraccion | None = None,
) -> dict[str, Any]:
    """Extrae el inventario recursivo, determinista y fail-closed sobre una copia aislada verificada de ``exe`` (F17)."""
    lim = limites or LimitesDeExtraccion()
    if not exe.is_file():
        raise InventarioError(f"El ejecutable no existe o no es un archivo regular: {exe}")

    entradas: list[dict[str, Any]] = []
    opacos: list[dict[str, str]] = []
    opciones_runtime: list[str] = []
    total_extraido = [0]
    python_version_binary = ""

    snap_path, exe_sha256, exe_bytes = _crear_copia_aislada_verificada(exe)
    try:
        if registro_sha256 is not None:
            _validar_registro_sha256(registro_sha256, exe_sha256, exe_bytes)

        with snap_path.open("rb") as fp:
            start_offset, end_offset, pyvers_cookie, pylib_name, toc_crudo = _prevalidar_carchive_crudo(fp, exe_bytes)

            fp.seek(0, os.SEEK_SET)
            bootloader_bytes = fp.read(start_offset)
            if len(bootloader_bytes) != start_offset:
                raise InventarioError("Lectura truncada del segmento bootloader")
            bootloader_sha256 = _sha256_bytes(bootloader_bytes)

            for nombre, entry_off, data_len, uncomp_len, comp_flag, typecode in toc_crudo:
                if typecode == PKG_ITEM_RUNTIME_OPTION:
                    opciones_runtime.append(nombre)
                    continue

                _verificar_tope_entradas(entradas, lim)
                if uncomp_len > lim.max_entry_bytes:
                    raise InventarioError(
                        f"Límite de descompresión excedido en '{nombre}': declarado={uncomp_len} max={lim.max_entry_bytes}"
                    )

                fp.seek(start_offset + entry_off, os.SEEK_SET)
                packed_raw = fp.read(data_len)
                if len(packed_raw) != data_len:
                    raise InventarioError(f"Entrada truncada en CArchive: '{nombre}'")
                packed_sha = _sha256_bytes(packed_raw)

                extracted_raw = (
                    _descomprimir_acotado(packed_raw, lim.max_entry_bytes, nombre) if comp_flag else packed_raw
                )
                if uncomp_len > 0 and len(extracted_raw) != uncomp_len:
                    raise InventarioError(
                        f"Tamaño descomprimido declarado ({uncomp_len}) distinto del extraído ({len(extracted_raw)}) en '{nombre}'"
                    )

                total_extraido[0] += len(extracted_raw)
                if total_extraido[0] > lim.max_total_bytes:
                    raise InventarioError(
                        f"Límite total de descompresión ({lim.max_total_bytes} B) excedido en '{nombre}'"
                    )

                norm = _normalizar_ruta(nombre)
                ext = Path(norm).suffix.casefold()
                if not python_version_binary and re.fullmatch(r"python3\d+\.dll", Path(norm).name.casefold()):
                    python_version_binary = _extraer_version_pe_cpython(extracted_raw)
                cat, orig, der = _clasificar_entrada_base(norm, typecode, "CArchive")
                if cat == CategoriaComponente.OPAQUE_CONTAINER.value:
                    opacos.append(
                        {"container": "CArchive", "name": norm, "reason": f"contenedor opaco ({typecode}/{ext})"}
                    )

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

                # Inspección recursiva de PYZ embebido (SBOM-01 + SBOM-02 + F06 + F18)
                if typecode == PKG_ITEM_PYZ:
                    if len(extracted_raw) > lim.max_entry_bytes:
                        raise InventarioError(f"PYZ '{norm}' excede límite de tamaño")
                    pyz_entradas = _parsear_pyz_seguro(extracted_raw, norm, lim)
                    pyz_cont = f"PYZ:{norm}"
                    for mod_nombre, ispkg, mod_pos, mod_len in pyz_entradas:
                        _verificar_tope_entradas(entradas, lim)
                        if mod_len > lim.max_entry_bytes:
                            raise InventarioError(f"Módulo PYZ '{mod_nombre}' excede límite de descompresión")
                        mod_packed = extracted_raw[mod_pos : mod_pos + mod_len]
                        if len(mod_packed) != mod_len:
                            raise InventarioError(f"Módulo PYZ truncado: '{mod_nombre}'")
                        # En PyInstaller 6, PYZ_ITEM_NSPKG (ispkg == 3) es paquete namespace PEP 420 con mod_len == 0 sin flujo zlib
                        mod_extracted = (
                            b""
                            if (mod_len == 0 and ispkg in (2, 3))
                            else _descomprimir_acotado(mod_packed, lim.max_entry_bytes, mod_nombre)
                        )
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
    finally:
        snap_path.unlink(missing_ok=True)

    entradas.sort(key=lambda item: (str(item["container"]), str(item["name"]), str(item["typecode"])))
    opacos.sort(key=lambda item: (item["container"], item["name"]))
    opciones_runtime.sort()

    return {
        "schema_version": 1,
        "trust_model": {
            "certified_scope": "controlled_first_party_builds_only",
            "untrusted_exe_policy": "requires_external_os_sandbox_isolation",
            "pyz_toc_deserializer": "marshal.loads_bounded_by_max_pyz_toc_bytes_not_safe_for_untrusted_input",
            "analysis_mode": "isolated_snapshot_copy",
        },
        "exe": {
            "name": exe.name,
            "bytes": exe_bytes,
            "sha256": exe_sha256,
            "python_version_cookie": pyvers_cookie,
            "python_version_binary": python_version_binary,
            "python_dll": pylib_name,
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
    """Persiste ``inventory.json`` de forma atómica, determinista y fail-closed (F06)."""
    try:
        if destino.is_dir():
            raise OSError(f"El destino es un directorio: {destino}")
        destino.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(inventario, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        fd, tmp_name = tempfile.mkstemp(dir=destino.parent, prefix=f".{destino.name}.", suffix=".tmp")
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
            os.replace(tmp_path, destino)
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise
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
        # F06: no dejar un inventario previo válido si esta ejecución falló
        if args.out.is_file():
            args.out.unlink(missing_ok=True)
        print(f"inventario_ejecutable: {exc}", file=sys.stderr)
        return EXIT_VALIDACION
    print(f"inventario_ejecutable: OK entries={inventario['entry_count']} sha256={inventario['exe']['sha256']}")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())

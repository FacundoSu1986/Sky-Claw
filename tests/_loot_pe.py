"""``LOOT.exe`` falso con recurso ``VERSIONINFO`` real para tests (PR-2, hardening).

``LOOTRunner`` atestigua la versión leyendo el ``VS_FIXEDFILEINFO`` del binario
antes de lanzarlo (:mod:`sky_claw.local.loot.binary_version`): un archivo vacío
ya no sirve como "LOOT.exe" de un runner con data root aislado, porque sin
versión atestiguada LOOT no se lanza (fail-closed).

:func:`pe_con_version` arma un PE mínimo pero estructuralmente válido —
cabecera DOS, firma PE, COFF, optional header PE32/PE32+, una sección
``.rsrc`` y el árbol tipo → nombre → idioma → dato del recurso ``RT_VERSION`` —
con la misma forma que produce ``rc.exe`` para ``1 VERSIONINFO`` de
``loot/loot`` ``src/gui/resource.rc``. Validado en desarrollo contra
``pefile`` (parser independiente): mismo ``FILEVERSION``/``PRODUCTVERSION``.
"""

from __future__ import annotations

import pathlib
import struct

_RVA_RSRC = 0x1000
_ALINEACION_ARCHIVO = 0x200
_SUBDIRECTORIO = 0x8000_0000
FIRMA_VS_FIXEDFILEINFO = 0xFEEF04BD

Version4 = tuple[int, int, int, int]


def _alinear(valor: int, alineacion: int) -> int:
    return (valor + alineacion - 1) // alineacion * alineacion


def _ms_ls(version: Version4) -> tuple[int, int]:
    return (version[0] << 16) | version[1], (version[2] << 16) | version[3]


def _vs_versioninfo(file_version: Version4, product_version: Version4, *, firma: int, clave: str) -> bytes:
    clave_utf16 = (clave + "\x00").encode("utf-16-le")
    cuerpo = struct.pack("<HH", 52, 0) + clave_utf16  # wValueLength, wType, szKey
    encabezado = 2 + len(cuerpo)
    relleno = (-encabezado) % 4
    file_ms, file_ls = _ms_ls(file_version)
    product_ms, product_ls = _ms_ls(product_version)
    fija = struct.pack(
        "<13I", firma, 0x0001_0000, file_ms, file_ls, product_ms, product_ls, 0x3F, 0, 0x0004_0004, 1, 0, 0, 0
    )
    total = encabezado + relleno + len(fija)
    return struct.pack("<H", total) + cuerpo + b"\x00" * relleno + fija


def _directorio(entradas: list[tuple[int, int]]) -> bytes:
    cabecera = struct.pack("<IIHHHH", 0, 0, 0, 0, 0, len(entradas))
    return cabecera + b"".join(struct.pack("<II", ident, destino) for ident, destino in entradas)


def pe_con_version(
    file_version: Version4 = (0, 29, 1, 0),
    product_version: Version4 | None = None,
    *,
    pe32plus: bool = True,
    tipo_recurso: int = 16,
    idiomas: int = 1,
    firma: int = FIRMA_VS_FIXEDFILEINFO,
    clave: str = "VS_VERSION_INFO",
) -> bytes:
    """Bytes de un PE con UN recurso ``VERSIONINFO`` (o variantes adversariales).

    Args:
        file_version / product_version: ``FILEVERSION`` / ``PRODUCTVERSION``
            (``product_version`` None = igual a ``file_version``).
        pe32plus: optional header PE32+ (x64, como LOOT 0.29) o PE32.
        tipo_recurso: id del tipo del único recurso (16 = ``RT_VERSION``).
        idiomas: entradas en el nivel idioma (>1 = recurso ambiguo).
        firma / clave: ``dwSignature`` / ``szKey`` del ``VS_VERSIONINFO``.
    """
    blob = _vs_versioninfo(file_version, product_version or file_version, firma=firma, clave=clave)
    offset_nombre = 24
    offset_idioma = 48
    offset_dato = offset_idioma + 16 + 8 * idiomas
    offset_blob = offset_dato + 16
    recursos = (
        _directorio([(tipo_recurso, _SUBDIRECTORIO | offset_nombre)])
        + _directorio([(1, _SUBDIRECTORIO | offset_idioma)])
        + _directorio([(0x409 + indice, offset_dato) for indice in range(idiomas)])
        + struct.pack("<IIII", _RVA_RSRC + offset_blob, len(blob), 0, 0)
        + blob
    )
    tamano_crudo = _alinear(len(recursos), _ALINEACION_ARCHIVO)

    tamano_opcional = 240 if pe32plus else 224
    opcional = bytearray(tamano_opcional)
    struct.pack_into("<H", opcional, 0, 0x20B if pe32plus else 0x10B)
    struct.pack_into("<II", opcional, 32, 0x1000, _ALINEACION_ARCHIVO)  # SectionAlignment, FileAlignment
    struct.pack_into("<II", opcional, 56, _RVA_RSRC + _alinear(len(recursos), 0x1000), _ALINEACION_ARCHIVO)
    struct.pack_into("<H", opcional, 68, 2)  # IMAGE_SUBSYSTEM_WINDOWS_GUI (LOOT es GUI)
    offset_cantidad, offset_directorios = (108, 112) if pe32plus else (92, 96)
    struct.pack_into("<I", opcional, offset_cantidad, 16)
    struct.pack_into("<II", opcional, offset_directorios + 16, _RVA_RSRC, len(recursos))

    dos = b"MZ" + b"\x00" * 58 + struct.pack("<I", 0x40)
    coff = struct.pack("<HHIIIHH", 0x8664 if pe32plus else 0x14C, 1, 0, 0, 0, tamano_opcional, 0x22)
    seccion = struct.pack(
        "<8sIIIIIIHHI", b".rsrc", len(recursos), _RVA_RSRC, tamano_crudo, _ALINEACION_ARCHIVO, 0, 0, 0, 0, 0x4000_0040
    )
    cabeceras = dos + b"PE\x00\x00" + coff + bytes(opcional) + seccion
    return cabeceras.ljust(_ALINEACION_ARCHIVO, b"\x00") + recursos.ljust(tamano_crudo, b"\x00")


def escribir_loot_exe(path: pathlib.Path, version: tuple[int, int, int] = (0, 29, 1)) -> pathlib.Path:
    """Escribe en *path* un ``LOOT.exe`` falso cuya versión atestiguable es *version*."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(pe_con_version((*version, 0)))
    return path

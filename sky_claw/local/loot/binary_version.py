"""Versión REAL de ``LOOT.exe`` leída de su recurso ``VERSIONINFO`` (PR-2, hardening).

Para qué: una corrida ``--auto-sort`` desatendida sobre un data root cuyo
``lastVersion`` no coincide con la versión del binario queda colgada en el modal
"First-Time Tips" hasta el timeout. :mod:`sky_claw.local.loot.headless_settings`
siembra ``lastVersion`` con el valor que devuelve este módulo, así que el valor
tiene que ser EXACTAMENTE el que LOOT compara, atestiguado desde el binario que
se va a lanzar — nunca adivinado ni tomado de configuración.

Evidencia upstream — ``loot/loot`` tag ``0.29.1``, commit
``77f3ba98966819fd6d92d97dcb2dbc4c1b9fb9b9``:

* ``src/gui/qt/main_window.cpp:366-368`` (``MainWindow::initialise``):
  ``if (getLastVersion() != getLootVersion()) showFirstRunDialog();`` — string
  exacto, sin normalizar. ``showFirstRunDialog`` (l.1311-1382) termina en
  ``messageBox.exec()`` (modal), ANTES de ``loadGame`` y por lo tanto antes del
  auto-sort.
* ``src/gui/version.cpp.in:27-33``: ``getLootVersion()`` es
  ``to_string(MAJOR) + '.' + to_string(MINOR) + '.' + to_string(PATCH)``
  (decimal, sin ceros a la izquierda, sin revisión), con las constantes de
  ``src/gui/version.h:30-32``.
* ``src/gui/resource.rc:3-5,15,20``: ``1 VERSIONINFO`` con
  ``FILEVERSION 0, 29, 1, 0`` y ``PRODUCTVERSION 0, 29, 1, 0``; el ``.rc`` se
  compila dentro de ``LOOT.exe`` (``CMakeLists.txt:365``).
* ``scripts/set_version_number.py``: el release escribe la MISMA versión de
  tres partes en ``version.h`` (``update_cpp_file``) y en ``resource.rc``
  (``update_resource_file``: ``VERSION a, b, c`` conservando el ``, 0`` final).
  Por eso ``FILEVERSION`` = ``(MAJOR, MINOR, PATCH, 0)`` y
  ``"MAJOR.MINOR.PATCH"`` es exactamente ``getLootVersion()``.
* LOOT 0.29.2 (``0402143e211ee20352c10965fa1a36df5edc63d5``): mismo esquema
  (``resource.rc`` ``0, 29, 2, 0``; ``version.h`` ``PATCH = 2``) y misma
  comparación en ``main_window.cpp:391-392``.

Por qué NO ``LOOT.exe --version``: ``main.cpp:90-95`` consulta el mutex
``LOOT.Shell.Instance`` ANTES de crear ``QApplication`` y el parser; con otra
instancia viva sale 0 sin imprimir nada — el mismo falso verde que PR-2 cierra
— y además es otro lanzamiento del GUI. Leer el recurso no ejecuta nada.

Contrato fail-closed: cualquier cosa que no sea un PE con un único
``VS_VERSIONINFO`` bien formado, ``FILEVERSION == PRODUCTVERSION`` y cuarto
componente ``0`` (el esquema de arriba) → :class:`LootBinaryVersionError`, y
el runner NO lanza LOOT.
"""

from __future__ import annotations

import pathlib
import struct
from typing import Final

#: Índice del directorio de recursos en el ``DataDirectory`` del optional header.
_IMAGE_DIRECTORY_ENTRY_RESOURCE: Final[int] = 2
#: ``RT_VERSION`` (``winuser.h``).
_RT_VERSION: Final[int] = 16
#: Bit alto de ``OffsetToData``: la entrada apunta a un subdirectorio.
_SUBDIRECTORIO: Final[int] = 0x8000_0000
#: ``VS_FIXEDFILEINFO.dwSignature``.
_FIRMA_VS_FIXEDFILEINFO: Final[int] = 0xFEEF04BD
_CLAVE_VS_VERSION_INFO: Final[bytes] = "VS_VERSION_INFO\x00".encode("utf-16-le")
#: ``wLength`` + ``wValueLength`` + ``wType`` + clave, alineado a 32 bits.
_OFFSET_VS_FIXEDFILEINFO: Final[int] = 40
_TAMANO_VS_FIXEDFILEINFO: Final[int] = 52
#: Techo de lectura: un ``LOOT.exe`` real pesa decenas de MB.
_TAMANO_MAXIMO: Final[int] = 512 * 1024 * 1024

Version4 = tuple[int, int, int, int]


class LootBinaryVersionError(RuntimeError):
    """La versión de ``LOOT.exe`` no se pudo atestiguar: LOOT NO debe lanzarse."""


class _PeMalformadoError(Exception):
    """Interno: estructura PE/VERSIONINFO ausente, truncada o ambigua."""


def read_loot_binary_version(loot_exe: pathlib.Path) -> str:
    """Devuelve ``"MAJOR.MINOR.PATCH"`` tal como lo produce ``getLootVersion()``.

    Raises:
        LootBinaryVersionError: el archivo no se puede leer, no es un PE, no
            tiene un único ``VS_VERSIONINFO`` válido o su versión no sigue el
            esquema de release de LOOT (ver docstring del módulo).
    """
    try:
        if loot_exe.stat().st_size > _TAMANO_MAXIMO:
            raise LootBinaryVersionError(f"{loot_exe} excede {_TAMANO_MAXIMO} bytes: no es un LOOT.exe plausible.")
        data = loot_exe.read_bytes()
    except OSError as exc:
        raise LootBinaryVersionError(f"No se pudo leer {loot_exe} para atestiguar su versión: {exc}") from exc
    try:
        file_version, product_version = _versiones_fijas(data)
    except _PeMalformadoError as exc:
        raise LootBinaryVersionError(
            f"No se pudo atestiguar la versión de {loot_exe}: {exc}. Sin versión exacta no se puede "
            "sembrar lastVersion y LOOT quedaría en el modal 'First-Time Tips' (loot/loot 0.29.1 "
            "main_window.cpp:366-368); no se lanza."
        ) from None
    if file_version != product_version:
        raise LootBinaryVersionError(
            f"{loot_exe}: FILEVERSION {_texto(file_version)} != PRODUCTVERSION {_texto(product_version)}; "
            "versión ambigua, no se lanza."
        )
    major, minor, patch, build = file_version
    if build != 0:
        raise LootBinaryVersionError(
            f"{loot_exe}: FILEVERSION {_texto(file_version)} no sigue el esquema de release de LOOT "
            "(a, b, c, 0 — scripts/set_version_number.py); no se puede derivar getLootVersion()."
        )
    return f"{major}.{minor}.{patch}"


def _texto(version: Version4) -> str:
    return ".".join(str(parte) for parte in version)


def _u16(data: bytes, offset: int) -> int:
    if offset < 0 or offset + 2 > len(data):
        raise _PeMalformadoError("estructura PE truncada")
    return int.from_bytes(data[offset : offset + 2], "little")


def _u32(data: bytes, offset: int) -> int:
    if offset < 0 or offset + 4 > len(data):
        raise _PeMalformadoError("estructura PE truncada")
    return int.from_bytes(data[offset : offset + 4], "little")


def _versiones_fijas(data: bytes) -> tuple[Version4, Version4]:
    """``(FILEVERSION, PRODUCTVERSION)`` del único ``VS_FIXEDFILEINFO`` del PE."""
    blob = _recurso_de_version(data)
    if len(blob) < _OFFSET_VS_FIXEDFILEINFO + _TAMANO_VS_FIXEDFILEINFO:
        raise _PeMalformadoError("VERSIONINFO truncado")
    largo_total = _u16(blob, 0)
    largo_valor = _u16(blob, 2)
    if blob[6 : 6 + len(_CLAVE_VS_VERSION_INFO)] != _CLAVE_VS_VERSION_INFO:
        raise _PeMalformadoError("el recurso RT_VERSION no empieza con la clave VS_VERSION_INFO")
    if largo_valor != _TAMANO_VS_FIXEDFILEINFO:
        raise _PeMalformadoError("VERSIONINFO sin VS_FIXEDFILEINFO")
    if not _OFFSET_VS_FIXEDFILEINFO + _TAMANO_VS_FIXEDFILEINFO <= largo_total <= len(blob):
        raise _PeMalformadoError("VERSIONINFO con longitud inconsistente")
    campos = struct.unpack_from("<13I", blob, _OFFSET_VS_FIXEDFILEINFO)
    if campos[0] != _FIRMA_VS_FIXEDFILEINFO:
        raise _PeMalformadoError("firma de VS_FIXEDFILEINFO inválida")
    file_ms, file_ls, product_ms, product_ls = campos[2:6]
    return _cuadrupla(file_ms, file_ls), _cuadrupla(product_ms, product_ls)


def _cuadrupla(ms: int, ls: int) -> Version4:
    return (ms >> 16, ms & 0xFFFF, ls >> 16, ls & 0xFFFF)


def _recurso_de_version(data: bytes) -> bytes:
    """Bytes del ÚNICO recurso ``RT_VERSION`` (tipo → nombre → idioma → datos)."""
    if data[:2] != b"MZ":
        raise _PeMalformadoError("no es un ejecutable PE (falta la firma MZ)")
    pe = _u32(data, 0x3C)
    if data[pe : pe + 4] != b"PE\x00\x00":
        raise _PeMalformadoError("no es un ejecutable PE (falta la firma PE)")
    coff = pe + 4
    cantidad_secciones = _u16(data, coff + 2)
    tamano_opcional = _u16(data, coff + 16)
    opcional = coff + 20
    magic = _u16(data, opcional)
    if magic == 0x20B:  # PE32+
        offset_cantidad, offset_directorios = 108, 112
    elif magic == 0x10B:  # PE32
        offset_cantidad, offset_directorios = 92, 96
    else:
        raise _PeMalformadoError(f"optional header desconocido (magic {magic:#x})")
    entrada = offset_directorios + 8 * _IMAGE_DIRECTORY_ENTRY_RESOURCE
    cantidad_directorios = _u32(data, opcional + offset_cantidad)
    if cantidad_directorios <= _IMAGE_DIRECTORY_ENTRY_RESOURCE or entrada + 8 > tamano_opcional:
        raise _PeMalformadoError("el PE no declara directorio de recursos")
    rva_recursos = _u32(data, opcional + entrada)
    if rva_recursos == 0 or _u32(data, opcional + entrada + 4) == 0:
        raise _PeMalformadoError("el PE no tiene recursos (sin VERSIONINFO)")

    tabla = opcional + tamano_opcional
    secciones = [
        (_u32(data, base + 12), _u32(data, base + 16), _u32(data, base + 20))
        for base in (tabla + 40 * indice for indice in range(cantidad_secciones))
    ]

    def a_offset(rva: int, largo: int) -> int:
        # Sólo bytes presentes en el archivo (SizeOfRawData), nunca padding virtual.
        for direccion_virtual, tamano_crudo, puntero_crudo in secciones:
            if direccion_virtual <= rva and rva + largo <= direccion_virtual + tamano_crudo:
                offset = puntero_crudo + (rva - direccion_virtual)
                if offset + largo > len(data):
                    raise _PeMalformadoError("sección truncada")
                return offset
        raise _PeMalformadoError("RVA fuera de las secciones del archivo")

    def entradas(relativo: int) -> list[tuple[int, int]]:
        cabecera = a_offset(rva_recursos + relativo, 16)
        total = _u16(data, cabecera + 12) + _u16(data, cabecera + 14)
        inicio = cabecera + 16
        return [(_u32(data, inicio + 8 * i), _u32(data, inicio + 8 * i + 4)) for i in range(total)]

    def unico_subdirectorio(candidatos: list[int], nivel: str) -> int:
        if len(candidatos) != 1:
            detalle = "ausente" if not candidatos else f"ambiguo ({len(candidatos)} entradas)"
            raise _PeMalformadoError(f"recurso VERSIONINFO {detalle} en el nivel {nivel}")
        if not candidatos[0] & _SUBDIRECTORIO:
            raise _PeMalformadoError(f"árbol de recursos inválido en el nivel {nivel}")
        return candidatos[0] & ~_SUBDIRECTORIO

    # Un ID de tipo nunca tiene el bit alto (las entradas con nombre sí).
    por_tipo = unico_subdirectorio([destino for ident, destino in entradas(0) if ident == _RT_VERSION], "tipo")
    por_nombre = unico_subdirectorio([destino for _, destino in entradas(por_tipo)], "nombre")
    idiomas = [destino for _, destino in entradas(por_nombre)]
    if len(idiomas) != 1 or idiomas[0] & _SUBDIRECTORIO:
        raise _PeMalformadoError("recurso VERSIONINFO ambiguo o inválido en el nivel idioma")
    dato = a_offset(rva_recursos + idiomas[0], 16)
    rva_dato = _u32(data, dato)
    tamano_dato = _u32(data, dato + 4)
    inicio_dato = a_offset(rva_dato, tamano_dato)
    return data[inicio_dato : inicio_dato + tamano_dato]

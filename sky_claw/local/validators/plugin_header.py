"""Lectura del header TES4 de un plugin — parser binario canónico (T-17 seed).

Lee el record ``TES4`` (siempre el primero de un ``.esp``/``.esm``/``.esl``)
sin xEdit ni LOOT y sin cargar el archivo completo: solo el header (24 bytes +
``dataSize``). Devuelve lo que el preflight necesita saber ANTES de tocar nada
— los masters declarados y los flags reales del record (master/light).

Es la pieza compartida que consumen los sensores del preflight:
:mod:`missing_masters` (masters) y :mod:`plugin_limits` (pools full/light). Un
único parser evita divergencias; es el germen del ``PluginHeaderInspector`` de
T-17 (ESL real, versión de header 43/44, FormIDs).

Formato del record TES4 (Skyrim SE): ``TES4`` + ``dataSize`` (u32) + ``flags``
(u32) + ``formID`` (u32) + ``vc`` (u32) + ``formVersion`` (u16) + ``vc2``
(u16); luego subrecords ``firma(4) + size(u16) + data``. Cada master es un
``MAST`` (zstring cp1252). El record TES4 nunca está comprimido.
"""

from __future__ import annotations

import pathlib
from dataclasses import dataclass

#: Tamaño del header del record TES4 en Skyrim SE.
_TES4_HEADER_SIZE = 24

#: Límite defensivo para el payload del record TES4: los masters viven en el
#: header (pocos KiB); 1 MiB deja margen sin permitir lecturas abusivas ante un
#: ``dataSize`` corrupto o malicioso.
_MAX_TES4_DATA_SIZE = 1024 * 1024

#: Flags del record TES4 relevantes para clasificar el plugin.
_FLAG_MASTER = 0x00000001  # ESM: carga como master (afecta el rank de orden).
_FLAG_LIGHT = 0x00000200  # ESL/ESPFE: consume slot del pool ligero (FE), no full.


class PluginHeaderError(Exception):
    """El archivo no tiene un header TES4 legible (truncado o no es un plugin)."""


@dataclass(frozen=True, slots=True)
class PluginHeader:
    """Datos del header TES4 que el preflight necesita.

    Attributes:
        masters: Masters declarados (subrecords ``MAST``), en orden.
        is_master: Flag ESM (0x1) — carga como master.
        is_light: Flag light/ESL (0x200) — cuenta contra el pool ligero (FE),
            aunque la extensión sea ``.esp`` (caso ESPFE, muy común).
    """

    masters: list[str]
    is_master: bool
    is_light: bool


def read_plugin_header(plugin: pathlib.Path) -> PluginHeader:
    """Lee el header TES4 de ``plugin`` (masters + flags), sin abrir el cuerpo.

    Raises:
        PluginHeaderError: Si el archivo está truncado o no es un plugin TES4.
    """
    try:
        with plugin.open("rb") as fh:
            head = fh.read(_TES4_HEADER_SIZE)
            if len(head) < _TES4_HEADER_SIZE or head[:4] != b"TES4":
                raise PluginHeaderError(f"{plugin.name}: no tiene un header TES4 válido.")
            data_size = int.from_bytes(head[4:8], "little")
            flags = int.from_bytes(head[8:12], "little")
            if data_size > _MAX_TES4_DATA_SIZE:
                raise PluginHeaderError(f"{plugin.name}: header TES4 demasiado grande ({data_size} bytes).")
            data = fh.read(data_size)
    except OSError as exc:
        raise PluginHeaderError(f"{plugin.name}: no se pudo leer ({exc}).") from exc
    if len(data) < data_size:
        raise PluginHeaderError(f"{plugin.name}: header TES4 truncado.")

    return PluginHeader(
        masters=_parse_masters(plugin.name, data),
        is_master=bool(flags & _FLAG_MASTER),
        is_light=bool(flags & _FLAG_LIGHT),
    )


def _parse_masters(name: str, data: bytes) -> list[str]:
    """Recorre los subrecords del header y extrae los ``MAST`` (zstring cp1252)."""
    masters: list[str] = []
    offset = 0
    xxxx_size: int | None = None
    while offset < len(data):
        if offset + 6 > len(data):
            raise PluginHeaderError(f"{name}: subrecord TES4 truncado.")
        sig = data[offset : offset + 4]
        size = int.from_bytes(data[offset + 4 : offset + 6], "little")
        offset += 6
        if sig == b"XXXX":
            if size != 4:
                raise PluginHeaderError(f"{name}: subrecord XXXX inválido.")
            if offset + 4 > len(data):
                raise PluginHeaderError(f"{name}: subrecord XXXX truncado.")
            # XXXX extiende el tamaño del subrecord siguiente (raro en TES4,
            # pero el formato lo permite — manejo defensivo).
            xxxx_size = int.from_bytes(data[offset : offset + 4], "little")
            offset += 4
            continue
        real_size = xxxx_size if xxxx_size is not None else size
        xxxx_size = None
        if offset + real_size > len(data):
            raise PluginHeaderError(f"{name}: subrecord TES4 truncado.")
        field = data[offset : offset + real_size]
        offset += real_size
        if sig == b"MAST":
            # zstring en windows-1252 (encoding clásico de los plugins).
            masters.append(field.rstrip(b"\x00").decode("cp1252", errors="replace"))
    if xxxx_size is not None:
        raise PluginHeaderError(f"{name}: subrecord XXXX sin campo siguiente.")
    return masters

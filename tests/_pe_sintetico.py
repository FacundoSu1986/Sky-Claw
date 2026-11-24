"""PE32+ sintéticos con forma de apphost .NET, para los tests de lanzamiento de Synthesis.

El repo no versiona ejecutables (``tests/test_clean_room_invariant.py``), así que los tests de
PR-3a (decisión CET) y PR-3b (shim) generan estos binarios en ``tmp_path``. La forma de las
estructuras se tomó de apphosts reales del rig (``MinimalPatcher.exe`` con y sin CET, un
single-file de .NET 10 y el ``Synthesis.Bethesda.CLI.exe``), medidos el 2026-11-22.
"""

from __future__ import annotations

import json
import pathlib
import struct

# Firma del encabezado de bundle que el SDK deja en todo apphost (HostWriter); los 8 bytes
# previos son el offset del bundle: 0 en un apphost simple, distinto de 0 en un single-file.
FIRMA_BUNDLE = bytes.fromhex("8b1202b96a612038727b930214d7a03213f5b9e6efae3318ee3b2dce24b36aae")
TAM_REGION_APP = 1025  # EMBED_MAX del apphost: nombre de la app + relleno de ceros
RUNTIMECONFIG_FD = {
    "runtimeOptions": {"tfm": "net10.0", "framework": {"name": "Microsoft.NETCore.App", "version": "10.0.0"}}
}

SIN = object()  # centinela: "no escribir esta estructura"


def pe_sintetico(
    *,
    cet: int | None | object = SIN,
    tam_cet: int = 4,
    entradas_cet: int = 1,
    offsets_bundle: tuple[int, ...] = (0,),
    apps: tuple[bytes, ...] = (),
    dir_debug: tuple[int, int] | None = None,
    maquina: int = 0x8664,
    subsistema: int = 3,
) -> bytes:
    """PE32+ mínimo que pefile parsea, con directorio de debug y marcadores de apphost."""
    seccion = bytearray()
    entradas: list[tuple[int, int]] = []  # (tamaño, offset relativo de los datos)
    datos_cet = bytearray()
    if cet is not SIN:
        for _ in range(entradas_cet):
            entradas.append((tam_cet, len(datos_cet)))
            datos_cet += struct.pack("<I", 0 if cet is None else int(cet))[:tam_cet].ljust(4, b"\0")
    n_entradas = len(entradas)
    base_datos = 28 * n_entradas
    va, raw = 0x1000, 0x200
    for tam, rel in entradas:
        seccion += struct.pack("<IIHHIIII", 0, 0, 0, 0, 20, tam, va + base_datos + rel, raw + base_datos + rel)
    seccion += datos_cet
    for off in offsets_bundle:
        seccion += b"\xaa" * 8 + struct.pack("<Q", off) + FIRMA_BUNDLE
    for app in apps:
        seccion += b"\x11\x11\x11\0" + app.ljust(TAM_REGION_APP, b"\0")
    seccion += b"\x11" * 16
    tam_raw = (len(seccion) + 0x1FF) & ~0x1FF
    seccion = seccion.ljust(tam_raw, b"\0")

    dos = bytearray(0x80)
    dos[0:2] = b"MZ"
    struct.pack_into("<I", dos, 0x3C, 0x80)
    # `maquina`: 0x8664 = x64, 0x14C = x86, 0xAA64 = ARM64 (IMAGE_FILE_HEADER.Machine).
    cabecera = struct.pack("<HHIIIHH", maquina, 1, 0, 0, 0, 0xF0, 0x22)
    dirs = [(0, 0)] * 16
    if n_entradas:
        dirs[6] = (va, 28 * n_entradas)
    if dir_debug is not None:
        dirs[6] = dir_debug  # campos inconsistentes a propósito
    opcional = struct.pack(
        "<HBBIIIIIQIIHHHHHHIIIIHHQQQQII",
        0x20B,
        14,
        0,
        tam_raw,
        0,
        0,
        va,
        va,
        0x140000000,
        0x1000,
        0x200,
        6,
        0,
        0,
        0,
        6,
        0,
        0,
        va + ((tam_raw + 0xFFF) & ~0xFFF),
        0x200,
        0,
        subsistema,  # 3 = CUI (consola), 2 = GUI
        0x8160,
        0x100000,
        0x1000,
        0x100000,
        0x1000,
        0,
        16,
    ) + b"".join(struct.pack("<II", *d) for d in dirs)
    seccion_hdr = struct.pack("<8sIIIIIIHHI", b".rdata", tam_raw, va, tam_raw, raw, 0, 0, 0, 0, 0x40000040)
    hdr = bytes(dos) + b"PE\0\0" + cabecera + opcional + seccion_hdr
    return hdr.ljust(raw, b"\0") + bytes(seccion)


def patcher_sintetico(
    carpeta: pathlib.Path,
    *,
    nombre: str = "MiPatcher",
    pe: bytes | None = None,
    dll: bool = True,
    runtimeconfig: object = RUNTIMECONFIG_FD,
) -> pathlib.Path:
    carpeta.mkdir(parents=True, exist_ok=True)
    exe = carpeta / f"{nombre}.exe"
    exe.write_bytes(pe if pe is not None else pe_sintetico(cet=1, apps=(f"{nombre}.dll".encode(),)))
    if dll:
        (carpeta / f"{nombre}.dll").write_bytes(b"MZ-no-importa")
    if runtimeconfig is not SIN:
        texto = runtimeconfig if isinstance(runtimeconfig, str) else json.dumps(runtimeconfig)
        (carpeta / f"{nombre}.runtimeconfig.json").write_text(texto, encoding="utf-8")
    return exe

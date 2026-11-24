"""Motor de Fase 1 de Synthesis (ADR 0019 §2.1/§2.4) — corte PR-3a: patchers con CETCompat.

Bajo MO2 2.5.2 / usvfs 0.5.6.1, un apphost .NET marcado CET-compatible
(``IMAGE_DLLCHARACTERISTICS_EX_CET_COMPAT``, default del SDK .NET 9+) cae con ``0xC0000005`` al
arrancar, aun con entorno completo; el mismo binario con ese bit apagado, o la misma app lanzada
como ``dotnet.exe <nombre>.dll`` (el muxer no tiene el flag), funciona. Decisión del propietario
(2026-11-22), en este orden:

1. Lo que Sky-Claw compila en Fase 1 se compila sin CET y **no incremental** (un build incremental
   conserva el apphost viejo con CET sin avisar), y después se verifica el apphost producido:
   :func:`argumentos_dotnet_build_patcher` y :func:`verificar_apphost_sin_cet`.
2. Un patcher ya compilado se clasifica con :func:`decidir_lanzamiento`: sin CET → directo; con CET,
   apphost simple, app embebida exactamente ``<nombre>.dll`` y despliegue framework-dependent →
   vía ``dotnet.exe``; cualquier otro caso → rechazo. Nunca se modifica el binario (rompería su
   pin de hash).

Todo lo que no se puede establecer cuenta como el caso inseguro: un ``runtimeconfig.json``
ilegible no es framework-dependent, una entrada CET ilegible no es "sin CET", y una app embebida
que no se encuentra exactamente una vez no se adivina.

Este módulo es puro y **síncrono** (lee archivos y parsea el PE): un consumidor async lo llama
con ``asyncio.to_thread``, nunca directo desde el event loop. Lo consumen la generación del shim y el traductor a
``CliPatcherSettings`` de los cortes siguientes de PR-3.
"""

from __future__ import annotations

import enum
import json
import mmap
import pathlib
import re
from collections.abc import Mapping
from dataclasses import dataclass

import pefile

__all__ = [
    "MODO_POR_MOTIVO",
    "CetCompatPersistenteError",
    "DecisionLanzamiento",
    "EstadoCet",
    "ModoLanzamiento",
    "Motivo",
    "FUENTE_NUGET_APROBABLE",
    "argumentos_dotnet_assembly_name",
    "argumentos_dotnet_build_patcher",
    "contenido_nuget_config_aprobado",
    "argumentos_dotnet_restore_patcher",
    "decidir_lanzamiento",
    "es_ejecutable_x64",
    "es_framework_dependent",
    "leer_estado_cet",
    "verificar_apphost_sin_cet",
]

# Tipo de entrada del directorio de debug con IMAGE_DLLCHARACTERISTICS_EX; bit 0 = CET_COMPAT.
_DEBUG_TYPE_EX_DLLCHARACTERISTICS = 20
_CET_COMPAT = 0x1
_INDICE_DIRECTORIO_DEBUG = pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_DEBUG"]

# Firma del encabezado de bundle que el SDK (HostWriter) deja en todo apphost. Los 8 bytes previos
# son el offset del bundle: 0 en un apphost simple, distinto de 0 en un single-file.
_FIRMA_BUNDLE = bytes.fromhex("8b1202b96a612038727b930214d7a03213f5b9e6efae3318ee3b2dce24b36aae")
# El apphost reserva EMBED_MAX = 1025 bytes para el nombre de la app; HostWriter escribe el nombre
# y el resto queda en ceros. Medido en apphosts reales del rig: exactamente una región así.
_TAM_REGION_APP = 1025
# `.dll` + NUL plegando SOLO mayúsculas ASCII, igual que la comparación final del nombre.
_SUFIJO_APP = re.compile(rb"\.[dD][lL][lL]\x00")


class EstadoCet(enum.Enum):
    """Lo que dice el PE sobre CET_COMPAT."""

    AUSENTE = "ausente"  # sin entrada de características extendidas: no es CET-compatible
    APAGADO = "apagado"
    ENCENDIDO = "encendido"
    ILEGIBLE = "ilegible"  # entrada presente pero inconsistente: se trata como encendido
    NO_ES_PE = "no_es_pe"


class ModoLanzamiento(enum.Enum):
    DIRECTO = "directo"
    VIA_DOTNET = "via_dotnet"
    RECHAZADO = "rechazado"


class Motivo(enum.Enum):
    """Cada camino de la decisión. Uno nuevo exige su caso en la tabla de tests."""

    SIN_CET = "sin_cet"
    CET_VIA_DOTNET = "cet_via_dotnet"
    NO_ES_PE = "no_es_pe"
    CET_ILEGIBLE = "cet_ilegible"
    NO_ES_APPHOST = "no_es_apphost"
    BUNDLE_SINGLE_FILE = "bundle_single_file"
    APP_EMBEBIDA_DISTINTA = "app_embebida_distinta"
    APP_EMBEBIDA_AMBIGUA = "app_embebida_ambigua"
    DLL_AUSENTE = "dll_ausente"
    NO_FRAMEWORK_DEPENDENT = "no_framework_dependent"


MODO_POR_MOTIVO: dict[Motivo, ModoLanzamiento] = {
    Motivo.SIN_CET: ModoLanzamiento.DIRECTO,
    Motivo.CET_VIA_DOTNET: ModoLanzamiento.VIA_DOTNET,
    Motivo.NO_ES_PE: ModoLanzamiento.RECHAZADO,
    Motivo.CET_ILEGIBLE: ModoLanzamiento.RECHAZADO,
    Motivo.NO_ES_APPHOST: ModoLanzamiento.RECHAZADO,
    Motivo.BUNDLE_SINGLE_FILE: ModoLanzamiento.RECHAZADO,
    Motivo.APP_EMBEBIDA_DISTINTA: ModoLanzamiento.RECHAZADO,
    Motivo.APP_EMBEBIDA_AMBIGUA: ModoLanzamiento.RECHAZADO,
    Motivo.DLL_AUSENTE: ModoLanzamiento.RECHAZADO,
    Motivo.NO_FRAMEWORK_DEPENDENT: ModoLanzamiento.RECHAZADO,
}


@dataclass(frozen=True)
class DecisionLanzamiento:
    """Cómo se lanza un patcher bajo USVFS. ``dll`` solo está presente en ``VIA_DOTNET``."""

    exe: pathlib.Path
    motivo: Motivo
    mensaje: str
    dll: pathlib.Path | None = None

    @property
    def modo(self) -> ModoLanzamiento:
        return MODO_POR_MOTIVO[self.motivo]


class CetCompatPersistenteError(Exception):
    """El apphost que produjo la Fase 1 no quedó demostrablemente sin CET (fail-closed)."""

    def __init__(self, exe: pathlib.Path, estado: EstadoCet) -> None:
        self.exe = exe
        self.estado = estado
        if estado is EstadoCet.NO_ES_PE and not exe.is_file():
            detalle = f"El apphost esperado {exe} no existe: el build no lo generó."
        else:
            detalle = f"El apphost compilado {exe} no quedó sin CETCompat (estado: {estado.value})."
        super().__init__(f"{detalle} Se aborta la Fase 1 (ADR 0019 §2.4): bajo USVFS caería con 0xC0000005.")


# --- lectura del PE ---------------------------------------------------------------------


def leer_estado_cet(exe: pathlib.Path) -> EstadoCet:
    """Lee el bit CET_COMPAT del apphost sin modificarlo."""
    try:
        pe = pefile.PE(str(exe), fast_load=True)
    except (OSError, pefile.PEFormatError):
        return EstadoCet.NO_ES_PE
    try:
        directorio = pe.OPTIONAL_HEADER.DATA_DIRECTORY[_INDICE_DIRECTORIO_DEBUG]
        if not directorio.VirtualAddress and not directorio.Size:
            return EstadoCet.AUSENTE
        if not directorio.VirtualAddress or not directorio.Size:
            # Un solo campo en cero es un directorio inconsistente: no permite afirmar "sin CET".
            return EstadoCet.ILEGIBLE
        pe.parse_data_directories(directories=[_INDICE_DIRECTORIO_DEBUG])
        entradas = getattr(pe, "DIRECTORY_ENTRY_DEBUG", None)
        if not entradas:
            # El directorio existe pero pefile no pudo leerlo: no se puede afirmar "sin CET".
            return EstadoCet.ILEGIBLE
        extendidas = [e for e in entradas if e.struct.Type == _DEBUG_TYPE_EX_DLLCHARACTERISTICS]
        if not extendidas:
            return EstadoCet.AUSENTE
        if len(extendidas) != 1:
            return EstadoCet.ILEGIBLE
        struct_ = extendidas[0].struct
        if struct_.SizeOfData < 4 or not struct_.AddressOfRawData:
            return EstadoCet.ILEGIBLE
        datos = pe.get_data(struct_.AddressOfRawData, 4)
        if len(datos) != 4:
            return EstadoCet.ILEGIBLE
        bits = int.from_bytes(datos, "little")
        return EstadoCet.ENCENDIDO if bits & _CET_COMPAT else EstadoCet.APAGADO
    except Exception:  # noqa: BLE001 — cualquier inconsistencia del PE cuenta como ilegible
        return EstadoCet.ILEGIBLE
    finally:
        pe.close()


_MAQUINA_X64 = 0x8664  # IMAGE_FILE_MACHINE_AMD64


def es_ejecutable_x64(exe: pathlib.Path) -> bool:
    """``True`` solo si *exe* es un PE cuyo ``FILE_HEADER.Machine`` es AMD64.

    La arquitectura se lee del binario, no de su ruta: un ``dotnet.exe`` x86 copiado fuera de
    ``Program Files (x86)`` no carga los runtimes x64 que piden los patchers. Ilegible → ``False``.
    """
    try:
        pe = pefile.PE(str(exe), fast_load=True)
    except (OSError, pefile.PEFormatError):
        return False
    try:
        return bool(pe.FILE_HEADER.Machine == _MAQUINA_X64)
    finally:
        pe.close()


_SUBSISTEMA_CONSOLA = 3  # IMAGE_SUBSYSTEM_WINDOWS_CUI


def es_ejecutable_de_consola(exe: pathlib.Path) -> bool:
    """``True`` solo si *exe* es un PE de consola (CUI). ``Synthesis.exe`` (GUI WPF) da ``False``."""
    try:
        pe = pefile.PE(str(exe), fast_load=True)
    except (OSError, pefile.PEFormatError):
        return False
    try:
        return bool(pe.OPTIONAL_HEADER.Subsystem == _SUBSISTEMA_CONSOLA)
    finally:
        pe.close()


def _offsets_de_bundle(datos: mmap.mmap) -> list[int]:
    offsets: list[int] = []
    pos = datos.find(_FIRMA_BUNDLE)
    while pos != -1:
        offsets.append(int.from_bytes(datos[pos - 8 : pos], "little") if pos >= 8 else -1)
        pos = datos.find(_FIRMA_BUNDLE, pos + 1)
    return offsets


def _apps_embebidas(datos: mmap.mmap) -> list[bytes]:
    """Regiones ``<nombre>.dll`` seguidas de ceros hasta completar los 1025 bytes del apphost."""
    apps: list[bytes] = []
    for coincidencia in _SUFIJO_APP.finditer(datos):
        pos = coincidencia.start()
        fin = coincidencia.end() - 1  # índice del NUL terminador
        inicio = datos.rfind(b"\x00", max(0, fin - _TAM_REGION_APP), pos) + 1
        limite = inicio + _TAM_REGION_APP
        if inicio < pos and limite <= len(datos) and datos[fin:limite].count(0) == limite - fin:
            apps.append(datos[inicio:fin])
    return apps


def es_framework_dependent(runtimeconfig: pathlib.Path) -> bool:
    """Framework-dependent solo si declara ``framework(s)`` y NO ``includedFrameworks``.

    Un self-contained también trae ``<nombre>.dll`` y ``runtimeconfig.json``, pero con
    ``includedFrameworks``. Archivo ausente, ilegible o con otra forma → ``False``.
    """
    try:
        contenido = json.loads(runtimeconfig.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return False
    if not isinstance(contenido, dict):
        return False
    opciones = contenido.get("runtimeOptions")
    if not isinstance(opciones, dict) or "includedFrameworks" in opciones:
        return False
    # Presencia por CLAVE, no por valor: `"frameworks": null` está presente (y es inválido).
    tiene_framework = "framework" in opciones
    tiene_frameworks = "frameworks" in opciones
    if not tiene_framework and not tiene_frameworks:
        return False
    framework = opciones.get("framework")
    frameworks = opciones.get("frameworks")
    # El host procesa ambas secciones cuando coexisten: TODA sección presente debe ser válida.
    if tiene_framework and not _referencia_de_framework_valida(framework):
        return False
    return not tiene_frameworks or (
        isinstance(frameworks, list)
        and bool(frameworks)
        and all(_referencia_de_framework_valida(f) for f in frameworks)
    )


def _referencia_de_framework_valida(referencia: object) -> bool:
    """El host de .NET exige ``name`` y ``version`` como strings no vacíos en cada referencia."""
    if not isinstance(referencia, dict):
        return False
    nombre = referencia.get("name")
    version = referencia.get("version")
    return isinstance(nombre, str) and bool(nombre) and isinstance(version, str) and bool(version)


# --- decisión ---------------------------------------------------------------------------


def _decision(exe: pathlib.Path, motivo: Motivo, mensaje: str, dll: pathlib.Path | None = None) -> DecisionLanzamiento:
    return DecisionLanzamiento(exe=exe, motivo=motivo, mensaje=mensaje, dll=dll)


def decidir_lanzamiento(exe: pathlib.Path) -> DecisionLanzamiento:
    """Decide cómo lanzar un patcher ya compilado bajo USVFS (ADR 0019 §2.4, punto 2)."""
    estado = leer_estado_cet(exe)
    if estado is EstadoCet.NO_ES_PE:
        return _decision(exe, Motivo.NO_ES_PE, f"{exe} no existe o no es un ejecutable PE válido.")
    if estado in (EstadoCet.AUSENTE, EstadoCet.APAGADO):
        return _decision(exe, Motivo.SIN_CET, f"{exe} no es CET-compatible: se lanza directo.")
    if estado is EstadoCet.ILEGIBLE:
        return _decision(
            exe, Motivo.CET_ILEGIBLE, f"No se pudo leer con certeza el flag CETCompat de {exe}: se rechaza."
        )

    # CET encendido: solo se admite el apphost simple de una app framework-dependent.
    nombre_dll = f"{exe.stem}.dll"
    try:
        with exe.open("rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as datos:
            offsets = _offsets_de_bundle(datos)
            if len(offsets) != 1 or offsets[0] < 0:
                return _decision(
                    exe,
                    Motivo.NO_ES_APPHOST,
                    f"{exe} tiene CETCompat y no es un apphost .NET reconocible: no se puede lanzar "
                    "vía dotnet.exe y bajo USVFS caería.",
                )
            if offsets[0] != 0:
                return _decision(
                    exe,
                    Motivo.BUNDLE_SINGLE_FILE,
                    f"{exe} es un single-file con CETCompat: bajo USVFS caería y no se puede lanzar vía "
                    "dotnet.exe. Recompilarlo con -p:CETCompat=false.",
                )
            apps = _apps_embebidas(datos)
    except (OSError, ValueError):
        return _decision(exe, Motivo.CET_ILEGIBLE, f"No se pudo releer {exe}: se rechaza.")
    if len(apps) != 1:
        return _decision(
            exe,
            Motivo.APP_EMBEBIDA_AMBIGUA,
            f"No se pudo establecer qué app lanza el apphost {exe} ({len(apps)} candidatas): se rechaza.",
        )
    # NTFS no distingue mayúsculas ASCII, así que `mipatcher.dll` es el mismo archivo que
    # `MiPatcher.dll`. `bytes.lower()` pliega SOLO ASCII: un plegado Unicode (`casefold`, que
    # iguala "ß" con "ss") podría aceptar un nombre que para NTFS es otro archivo.
    if apps[0].lower() != nombre_dll.encode("utf-8").lower():
        return _decision(
            exe,
            Motivo.APP_EMBEBIDA_DISTINTA,
            f"El apphost {exe} lanza {apps[0].decode('utf-8', 'replace')!r}, no {nombre_dll!r}: se rechaza.",
        )
    dll = exe.with_name(nombre_dll)
    if not dll.is_file():
        return _decision(exe, Motivo.DLL_AUSENTE, f"{exe} tiene CETCompat y falta {dll}: se rechaza.")
    if not es_framework_dependent(exe.with_name(f"{exe.stem}.runtimeconfig.json")):
        return _decision(
            exe,
            Motivo.NO_FRAMEWORK_DEPENDENT,
            f"{exe} tiene CETCompat y no es framework-dependent (self-contained o runtimeconfig ilegible): "
            "se rechaza. Recompilarlo con -p:CETCompat=false.",
        )
    return _decision(
        exe, Motivo.CET_VIA_DOTNET, f"{exe} tiene CETCompat: se lanza como dotnet.exe {nombre_dll}.", dll=dll
    )


# --- Fase 1: build sin CET --------------------------------------------------------------


#: Propiedades MSBuild extra que un build de la Fase 1 puede fijar (lista CERRADA). Las usa el
#: instalador del CLI (PR-3i): ``GitVersion.MsBuild`` de Synthesis exige un ``.git`` con historia, y la
#: caché de fuentes lo descarta; se apaga y la versión se fija a mano. Ninguna pisa un flag fijo.
PROPIEDADES_MSBUILD_ADMITIDAS = frozenset({"DisableGitVersionTask", "Version"})
_VALOR_DE_PROPIEDAD = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


def validar_propiedades_msbuild(propiedades: Mapping[str, str] | None) -> tuple[tuple[str, str], ...]:
    """Propiedades ordenadas; ``ValueError`` si alguna no está admitida o su valor no es un token seguro."""
    if not propiedades:
        return ()
    for clave, valor in propiedades.items():
        if clave not in PROPIEDADES_MSBUILD_ADMITIDAS:
            raise ValueError(f"Propiedad MSBuild no admitida en la Fase 1: {clave!r}")
        if not isinstance(valor, str) or not _VALOR_DE_PROPIEDAD.fullmatch(valor):
            raise ValueError(f"Valor inválido para la propiedad MSBuild {clave!r}: {valor!r}")
    return tuple(sorted(propiedades.items()))


def argumentos_dotnet_build_patcher(
    proyecto: pathlib.Path,
    *,
    salida: pathlib.Path | None = None,
    propiedades: Mapping[str, str] | None = None,
) -> list[str]:
    """Argumentos de ``dotnet`` para compilar un patcher en Fase 1 (tras el restore con HITL).

    ``--no-incremental`` es obligatorio: un build incremental conserva el apphost previo con CET
    aunque se pase ``-p:CETCompat=false`` (evidencia de rig 2026-11-22). ``propiedades`` solo admite
    :data:`PROPIEDADES_MSBUILD_ADMITIDAS`.
    """
    extra = [f"-p:{clave}={valor}" for clave, valor in validar_propiedades_msbuild(propiedades)]
    return [
        "build",
        str(proyecto),
        "--no-restore",
        "--no-incremental",
        "--disable-build-servers",
        "-c",
        "Release",
        "-p:UseSharedCompilation=false",
        "-p:CETCompat=false",
        *extra,
        *(["-o", str(salida)] if salida is not None else []),
    ]


#: Única fuente de paquetes que la Fase 1 deja usar al ``restore``, y la que se muestra en el pedido
#: HITL de egress: lo aprobado y lo efectivo salen de la misma constante.
FUENTE_NUGET_APROBABLE = "https://api.nuget.org/v3/index.json"


def contenido_nuget_config_aprobado() -> str:
    """``nuget.config`` de la Fase 1: borra toda fuente heredada y deja solo la aprobable."""
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        "<configuration>\n"
        "  <packageSources>\n"
        "    <clear />\n"
        f'    <add key="nuget.org" value="{FUENTE_NUGET_APROBABLE}" protocolVersion="3" />\n'
        "  </packageSources>\n"
        "  <fallbackPackageFolders>\n"
        "    <clear />\n"
        "  </fallbackPackageFolders>\n"
        "</configuration>\n"
    )


def argumentos_dotnet_restore_patcher(proyecto: pathlib.Path, *, configfile: pathlib.Path) -> list[str]:
    """Argumentos de ``dotnet restore`` de la Fase 1: abre red (NuGet), solo tras la compuerta HITL.

    - ``--configfile``: con él, NuGet usa SOLO ese archivo y no la jerarquía de ``NuGet.Config``
      de la solución del usuario, que podría agregar hosts que el operador no aprobó.
    - ``RestoreSources``/``RestoreAdditionalProjectSources`` como propiedades globales: le ganan a
      un ``Directory.Build.props`` del proyecto que intente agregar fuentes.
    - ``Configuration=Release``: el build corre con ``--no-restore`` en Release; restaurar en el
      default (Debug) dejaría sin assets a un patcher con dependencias por configuración.
    """
    return [
        "restore",
        str(proyecto),
        "--disable-build-servers",
        "--configfile",
        str(configfile),
        "-p:Configuration=Release",
        f"-p:RestoreSources={FUENTE_NUGET_APROBABLE}",
        "-p:RestoreAdditionalProjectSources=",
    ]


def argumentos_dotnet_assembly_name(proyecto: pathlib.Path) -> list[str]:
    """Evalúa ``AssemblyName`` con MSBuild (sin red): el apphost se llama ``$(AssemblyName).exe``."""
    return ["msbuild", str(proyecto), "-getProperty:AssemblyName", "-p:Configuration=Release", "-nologo"]


def verificar_apphost_sin_cet(exe: pathlib.Path) -> None:
    """Falla cerrado si el apphost producido no quedó demostrablemente sin CET."""
    estado = leer_estado_cet(exe)
    if estado not in (EstadoCet.AUSENTE, EstadoCet.APAGADO):
        raise CetCompatPersistenteError(exe, estado)

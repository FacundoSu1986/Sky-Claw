"""Shim de producción de los patchers de Synthesis (ADR 0019 §2.1/§2.4) — corte PR-3b.

En la Fase 2, ``Synthesis.Bethesda.CLI`` lanza cada ``CliPatcherSettings`` por su
``PathToExecutable``. La Fase 1 de Sky-Claw apunta ese campo a una **copia por patcher** del shim
C# de ``synthesis_shim_fuentes/``, con un ``shim.json`` al lado. Este módulo es el lado Python de
ese contrato:

- :func:`hash_arbol` fija el árbol completo del patcher (exe, DLLs, ``runtimeconfig.json``...): un
  pin del ``.exe`` solo deja correr una DLL alterada (evidencia de rig, spike F-02).
- :func:`construir_config_shim` reusa la decisión CET de PR-3a (:func:`decidir_lanzamiento`) y
  produce la configuración cerrada: directo, vía ``dotnet.exe exec <nombre>.dll`` o, si el patcher
  se rechaza, ningún shim.
- :func:`materializar_shim` copia los binarios del shim compilado y escribe su ``shim.json``.

El esquema es cerrado en los dos lados: :data:`CLAVES_CONFIG_SHIM` y ``ClavesPermitidas`` del
``Program.cs`` deben coincidir (``tests/test_synthesis_shim.py``).

Síncrono: lee y copia archivos. Un consumidor async lo llama con ``asyncio.to_thread``.

**Límite:** el shim verifica el hash y después lanza; entre ambas cosas un escritor del cache
podría cambiar el árbol (TOCTOU). Lo acota que el cache viva bajo
``SystemPaths.runtime_state_dir()``, escribible solo por el usuario de Sky-Claw.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import shutil
from collections.abc import Mapping

from sky_claw.app.security.links import exigir_arbol_copiable_sin_reparse
from sky_claw.local.tools.patcher_pipeline import is_x86_dotnet_path
from sky_claw.local.tools.synthesis_prebuild import (
    CetCompatPersistenteError,
    ModoLanzamiento,
    decidir_lanzamiento,
    verificar_apphost_sin_cet,
)

__all__ = [
    "CLAVES_CONFIG_SHIM",
    "ESQUEMA_CONFIG_SHIM",
    "MODO_SHIM_POR_MODO",
    "NOMBRE_CONFIG_SHIM",
    "NOMBRE_EJECUTABLE_SHIM",
    "ShimConfigError",
    "construir_config_shim",
    "directorio_fuentes_shim",
    "hash_arbol",
    "materializar_shim",
]

ESQUEMA_CONFIG_SHIM = 1
NOMBRE_CONFIG_SHIM = "shim.json"
NOMBRE_EJECUTABLE_SHIM = "SkyClawSynthesisShim.exe"
CLAVES_CONFIG_SHIM = frozenset(
    {
        "schema",
        "modo",
        "patcher",
        "dll",
        "dotnet",
        "arbolSha256",
        "internalDataFolder",
        "defaultDataFolderPath",
    }
)
MODO_SHIM_POR_MODO: dict[ModoLanzamiento, str | None] = {
    ModoLanzamiento.DIRECTO: "directo",
    ModoLanzamiento.VIA_DOTNET: "via_dotnet",
    ModoLanzamiento.RECHAZADO: None,
}

_DIRECTORIO_FUENTES = pathlib.Path(__file__).with_name("synthesis_shim_fuentes")
_MAX_BMP = 0xFFFF


class ShimConfigError(Exception):
    """No se puede construir o materializar el shim de un patcher (fail-closed)."""


def directorio_fuentes_shim() -> pathlib.Path:
    """Carpeta con ``Program.cs`` y el ``.csproj`` del shim (viaja en el wheel y en el congelado)."""
    return _DIRECTORIO_FUENTES


def hash_arbol(raiz: pathlib.Path) -> str:
    """sha256 sobre ``"<ruta_posix>\\0<sha256>\\n"`` por archivo, en orden de ruta.

    Mismo algoritmo que ``HashArbol`` del shim C#. Rechaza symlinks, junctions y cualquier
    entrada que no sea directorio real o archivo regular, y nombres fuera del BMP (el orden UTF-16
    de C# y el de code points de Python solo difieren ahí).
    """
    try:
        exigir_arbol_copiable_sin_reparse(raiz)
    except OSError as exc:
        raise ShimConfigError(f"El árbol del patcher {raiz} no es fijable: {exc}") from exc
    archivos: list[tuple[str, pathlib.Path]] = []

    def _fallar(exc: OSError) -> None:
        # os.walk ignora los errores de listado por defecto: un directorio ilegible quedaría
        # fuera del hash sin aviso.
        raise ShimConfigError(f"No se pudo listar el árbol del patcher {raiz}: {exc}") from exc

    for carpeta, subcarpetas, nombres in os.walk(raiz, onerror=_fallar, followlinks=False):
        subcarpetas.sort()
        # Se validan TODAS las entradas, carpetas incluidas, igual que el shim C#: una carpeta
        # vacía no aporta líneas al hash, pero el shim igual la rechazaría en la Fase 2.
        for nombre in (*subcarpetas, *nombres):
            rel = (pathlib.Path(carpeta) / nombre).relative_to(raiz).as_posix()
            if any(ord(c) > _MAX_BMP for c in rel):
                raise ShimConfigError(f"Nombre fuera del BMP en el árbol del patcher: {rel!r}")
        for nombre in nombres:
            ruta = pathlib.Path(carpeta) / nombre
            archivos.append((ruta.relative_to(raiz).as_posix(), ruta))
    acumulado = hashlib.sha256()
    for rel, ruta in sorted(archivos):
        digesto = hashlib.sha256()
        try:
            with ruta.open("rb") as flujo:
                for bloque in iter(lambda: flujo.read(1 << 20), b""):
                    digesto.update(bloque)
        except OSError as exc:
            raise ShimConfigError(f"No se pudo leer {rel} del árbol del patcher: {exc}") from exc
        acumulado.update(f"{rel}\0{digesto.hexdigest()}\n".encode())
    return acumulado.hexdigest()


def _exigir_ruta_absoluta(ruta: pathlib.Path, que: str) -> None:
    if not ruta.is_absolute():
        raise ShimConfigError(f"{que} debe ser una ruta absoluta: {ruta}")


def _validar_dotnet(dotnet: pathlib.Path | None) -> pathlib.Path:
    if dotnet is None:
        raise ShimConfigError("El patcher se lanza vía dotnet.exe y no se indicó el dotnet x64 verificado.")
    _exigir_ruta_absoluta(dotnet, "dotnet")
    if dotnet.name.casefold() != "dotnet.exe":
        raise ShimConfigError(f"dotnet no apunta a dotnet.exe: {dotnet}")
    if is_x86_dotnet_path(str(dotnet)):
        raise ShimConfigError(f"dotnet apunta a la instalación x86 (Program Files (x86)): {dotnet}")
    if not dotnet.is_file():
        raise ShimConfigError(f"dotnet.exe no existe: {dotnet}")
    return dotnet


def construir_config_shim(
    patcher: pathlib.Path,
    *,
    dotnet: pathlib.Path | None = None,
    internal_data_folder: pathlib.Path | None = None,
    default_data_folder_path: pathlib.Path | None = None,
) -> dict[str, object]:
    """Configuración cerrada del shim para *patcher*; decide el lanzamiento acá mismo.

    La decisión CET se toma adentro (y no se recibe hecha) para que nadie pueda pasar una
    decisión vieja o fabricada. Un patcher rechazado no tiene shim: :class:`ShimConfigError`
    con el mensaje de la decisión.
    """
    _exigir_ruta_absoluta(patcher, "patcher")
    if (internal_data_folder is None) != (default_data_folder_path is None):
        raise ShimConfigError("internalDataFolder y defaultDataFolderPath van juntos o no van.")
    for carpeta, que in (
        (internal_data_folder, "internalDataFolder"),
        (default_data_folder_path, "defaultDataFolderPath"),
    ):
        if carpeta is not None:
            _exigir_ruta_absoluta(carpeta, que)
            if not carpeta.is_dir():
                raise ShimConfigError(f"{que} no es una carpeta existente: {carpeta}")

    decision = decidir_lanzamiento(patcher)
    modo = MODO_SHIM_POR_MODO[decision.modo]
    if modo is None:
        raise ShimConfigError(decision.mensaje)
    dll: str | None = None
    ruta_dotnet: str | None = None
    if decision.modo is ModoLanzamiento.VIA_DOTNET:
        assert decision.dll is not None  # garantizado por DecisionLanzamiento
        dll = str(decision.dll)
        ruta_dotnet = str(_validar_dotnet(dotnet))
    return {
        "schema": ESQUEMA_CONFIG_SHIM,
        "modo": modo,
        "patcher": str(patcher),
        "dll": dll,
        "dotnet": ruta_dotnet,
        "arbolSha256": hash_arbol(patcher.parent),
        "internalDataFolder": None if internal_data_folder is None else str(internal_data_folder),
        "defaultDataFolderPath": None if default_data_folder_path is None else str(default_data_folder_path),
    }


def materializar_shim(binarios: pathlib.Path, destino: pathlib.Path, config: Mapping[str, object]) -> pathlib.Path:
    """Copia el shim compilado en *destino* (que no debe existir) y escribe su ``shim.json``.

    Devuelve la ruta del ``.exe`` del shim, que es lo que va al ``PathToExecutable`` del patcher.
    Verifica antes de copiar que el shim compilado no tenga CET (correría bajo USVFS igual que
    cualquier patcher) y que sus binarios sean un árbol sin enlaces. No borra nada: si *destino*
    ya existe falla, y limpiar el cache le toca al llamador (con ``rmtree_link_aware``).
    """
    if set(config) != CLAVES_CONFIG_SHIM or config.get("schema") != ESQUEMA_CONFIG_SHIM:
        raise ShimConfigError(f"Configuración del shim fuera de contrato: claves {sorted(config)}")
    if os.path.lexists(destino):
        raise ShimConfigError(f"El destino del shim ya existe: {destino}")
    try:
        exigir_arbol_copiable_sin_reparse(binarios)
        verificar_apphost_sin_cet(binarios / NOMBRE_EJECUTABLE_SHIM)
    except CetCompatPersistenteError as exc:
        raise ShimConfigError(str(exc)) from exc
    except OSError as exc:
        raise ShimConfigError(f"Los binarios del shim {binarios} no son copiables: {exc}") from exc

    destino.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(binarios, destino, symlinks=True)
    texto = json.dumps(dict(config), ensure_ascii=False, indent=2)
    temporal = destino / f"{NOMBRE_CONFIG_SHIM}.tmp"
    temporal.write_text(texto, encoding="utf-8")
    os.replace(temporal, destino / NOMBRE_CONFIG_SHIM)
    return destino / NOMBRE_EJECUTABLE_SHIM

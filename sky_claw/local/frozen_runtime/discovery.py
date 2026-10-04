"""Discovery determinista y fail-closed de la Managed Source (P1).

Decisiones de contrato (ADR 0012 §6/§18/§19 del pedido P1):

- P1 soporta ``provider="steam"`` (AppID 489830) y ``game_key="skyrimse"``.
- Evidencia de proveedor OBLIGATORIA: el candidato debe residir en
  ``<library>/steamapps/common/<game dir>`` y tener ``appmanifest_<appid>.acf``
  en ``<library>/steamapps/``. Una copia externa con ``SkyrimSE.exe`` NO se
  etiqueta como Managed Source de Steam (D07).
- Varias instalaciones válidas y distintas ⇒ AMBIGUOUS (fail-closed; sin
  first-match-wins) (D06).
- Enlaces (symlink/junction/reparse) en el root o sus ancestros ⇒ candidato
  inválido: una fuente redirigida no produce evidencia falsa (D-adversarial).
- El contenido del manifest es advisory para el discovery: su existencia es la
  evidencia de proveedor; su parseo lo maneja ``provider_signals``.

Todo el discovery es READ-ONLY sobre disco.
"""

from __future__ import annotations

import os
import pathlib

from sky_claw.config import SKYRIM_SE_APPID, STEAM_DEFAULT_PATHS
from sky_claw.local.frozen_runtime._vdf import parse_vdf_file
from sky_claw.local.frozen_runtime.errors import FrozenRuntimeError, MalformedVdfError
from sky_claw.local.frozen_runtime.independence import descripcion_de_enlace
from sky_claw.local.frozen_runtime.models import (
    CRITICAL_EXE_BY_GAME,
    GAME_DIR_NAME_BY_KEY,
    GAME_KEYS_SUPPORTED,
    DiscoveryState,
    ManagedSource,
    ManagedSourceDiscoveryResult,
    ManagedSourceProvider,
)


def _normcase_abspath(path: pathlib.Path) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _exe_presente(root: pathlib.Path, exe_name: str) -> bool:
    """True si ``exe_name`` existe como archivo regular propio (sin enlaces)."""
    nombre_buscado = exe_name.casefold()
    try:
        with os.scandir(root) as entries:
            for entry in entries:
                if entry.name.casefold() == nombre_buscado:
                    return entry.is_file(follow_symlinks=False)
    except OSError:
        return False
    return False


def _manifest_presente(library_steamapps: pathlib.Path, appid: str) -> bool:
    return (library_steamapps / f"appmanifest_{appid}.acf").is_file()


def _candidato_valido(root: pathlib.Path, game_key: str, appid: str, *, motivos: list[str]) -> ManagedSource | None:
    exe_name = CRITICAL_EXE_BY_GAME[game_key]
    motivo_links = descripcion_de_enlace(root)
    if motivo_links is not None:
        motivos.append(f"'{root}': {motivo_links}")
        return None
    if not root.is_dir():
        motivos.append(f"'{root}' no es un directorio")
        return None
    if not _exe_presente(root, exe_name):
        motivos.append(f"'{root}' no contiene '{exe_name}'")
        return None
    library_steamapps = root.parent.parent
    if library_steamapps.name.casefold() != "steamapps":
        motivos.append(f"'{root}' no está bajo un directorio steamapps")
        return None
    # Layout canónico completo: <library>/steamapps/common/<game dir>. Sin
    # esto, una copia manual en <library>/steamapps/backups/<x> se aceptaría
    # con el manifest de otra install (evidencia de proveedor falsa).
    if root.parent.name.casefold() != "common":
        motivos.append(f"'{root}' no está bajo <library>/steamapps/common")
        return None
    nombre_canonico = GAME_DIR_NAME_BY_KEY[game_key]
    if root.name.casefold() != nombre_canonico.casefold():
        motivos.append(f"'{root}' no tiene el nombre de instalación canónico '{nombre_canonico}'")
        return None
    if not _manifest_presente(library_steamapps, appid):
        motivos.append(f"sin evidencia de proveedor: falta appmanifest_{appid}.acf en '{library_steamapps}'")
        return None
    return ManagedSource(
        provider=ManagedSourceProvider.STEAM,
        game_key=game_key,
        appid=appid,
        root=root,
        library_steamapps=library_steamapps,
        library_root=library_steamapps.parent,
    )


def _library_paths_from_vdf_data(data: dict[str, object]) -> list[str]:
    """Extrae las rutas de library de las tres variantes conocidas del VDF.

    - antigua: pares top-level ``"path" "<ruta>"``;
    - intermedia: claves numéricas con valor ruta (``"1" "D:\\SteamLibrary"``);
    - moderna: ``"libraryfolders" { "0" { "path" "<ruta>" } ... }``.
    """
    paths: list[str] = []
    for key, value in data.items():
        if key.casefold() == "libraryfolders" and isinstance(value, dict):
            for entry_key, entry in value.items():
                if isinstance(entry, dict):
                    entry_path = entry.get("path")
                    if isinstance(entry_path, str) and entry_path:
                        paths.append(entry_path)
                elif isinstance(entry, str) and entry and entry_key.isdigit() and _parece_ruta_absoluta(entry):
                    # Formato intermedio: entradas numéricas escalares DENTRO
                    # del wrapper "libraryfolders" (no sólo en el root).
                    paths.append(entry)
        elif (
            isinstance(value, str)
            and value
            and (key.casefold() == "path" or (key.isdigit() and _parece_ruta_absoluta(value)))
        ):
            paths.append(value)
    return paths


def _parece_ruta_absoluta(value: str) -> bool:
    return len(value) >= 2 and (value[1] == ":" or value.startswith("\\\\"))


def _bibliotecas_de_steam_root(steam_root: pathlib.Path, *, motivos: list[str]) -> list[pathlib.Path]:
    """Libraries derivadas del root de Steam + ``libraryfolders.vdf`` si es legible.

    El root del cliente Steam siempre es library de sí mismo (evidencia en
    disco, no parseada). Un VDF malformado se registra y no se adivina ninguna
    library adicional (fail-closed, D04).
    """
    bibliotecas: list[pathlib.Path] = [pathlib.Path(steam_root)]
    vdf = pathlib.Path(steam_root) / "steamapps" / "libraryfolders.vdf"
    if not vdf.is_file():
        return bibliotecas
    try:
        data = parse_vdf_file(vdf)
    except MalformedVdfError as exc:
        motivos.append(f"libraryfolders.vdf ilegible en '{steam_root}': {exc}")
        return bibliotecas
    for raw in _library_paths_from_vdf_data(data):
        path = pathlib.Path(raw)
        if not path.is_absolute():
            motivos.append(f"ruta de library no absoluta en '{vdf}': '{raw}'")
            continue
        bibliotecas.append(path)
    return bibliotecas


def discover_managed_source(
    *,
    provider: ManagedSourceProvider = ManagedSourceProvider.STEAM,
    game_key: str = "skyrimse",
    appid: str = SKYRIM_SE_APPID,
    explicit_root: pathlib.Path | None = None,
    steam_roots: tuple[str, ...] = STEAM_DEFAULT_PATHS,
) -> ManagedSourceDiscoveryResult:
    """Descubre la Managed Source de forma determinista y fail-closed.

    Args:
        provider: proveedor soportado (P1: sólo STEAM).
        game_key: clave canónica del juego (P1: sólo ``skyrimse``).
        appid: AppID del proveedor (Steam: ``489830``).
        explicit_root: ruta configurada por el usuario a validar como Managed
            Source. Sin evidencia de proveedor ⇒ INVALID (nunca se etiqueta una
            copia externa como provider=steam).
        steam_roots: roots del cliente Steam a escanear (inyectable en tests).

    Returns:
        Resultado tipado; nunca elige silenciosamente entre candidatos.
    """
    if provider is not ManagedSourceProvider.STEAM:
        raise FrozenRuntimeError(f"proveedor no soportado en P1: '{provider}'")
    if game_key not in GAME_KEYS_SUPPORTED:
        raise FrozenRuntimeError(f"game_key no soportado en P1: '{game_key}'")
    motivos: list[str] = []

    if explicit_root is not None:
        root = pathlib.Path(explicit_root)
        if not root.is_absolute():
            # abspath (NO resolve): normaliza sin seguir symlinks/junctions, así
            # la validación de enlaces inspecciona la ruta ORIGINAL del usuario.
            root = pathlib.Path(os.path.abspath(os.fspath(root)))
        candidato = _candidato_valido(root, game_key, appid, motivos=motivos)
        if candidato is not None:
            return ManagedSourceDiscoveryResult(
                state=DiscoveryState.FOUND, message="Managed Source validada desde la ruta explícita", source=candidato
            )
        return ManagedSourceDiscoveryResult(
            state=DiscoveryState.INVALID,
            message="ruta explícita inválida como Managed Source: " + "; ".join(motivos),
            candidates=(root,),
        )

    game_dir = GAME_DIR_NAME_BY_KEY[game_key]
    vistos: dict[str, pathlib.Path] = {}
    validos: list[ManagedSource] = []
    for steam_root_raw in steam_roots:
        steam_root = pathlib.Path(steam_root_raw)
        if not steam_root.is_dir():
            continue
        for library in _bibliotecas_de_steam_root(steam_root, motivos=motivos):
            clave = _normcase_abspath(library)
            if clave in vistos:
                continue
            vistos[clave] = library
            root = library / "steamapps" / "common" / game_dir
            candidato = _candidato_valido(root, game_key, appid, motivos=motivos)
            if candidato is not None:
                validos.append(candidato)

    unicos: dict[str, ManagedSource] = {}
    for candidato in validos:
        clave = _normcase_abspath(candidato.root)
        if clave not in unicos:
            unicos[clave] = candidato

    if not unicos:
        detalle = f" ({'; '.join(motivos)})" if motivos else ""
        return ManagedSourceDiscoveryResult(
            state=DiscoveryState.NOT_FOUND,
            message=f"no se encontró una Managed Source de {provider.value}/{game_key}{detalle}",
        )
    if len(unicos) > 1:
        candidatas = tuple(sorted((c.root for c in unicos.values()), key=str))
        return ManagedSourceDiscoveryResult(
            state=DiscoveryState.AMBIGUOUS,
            message=(
                f"múltiples instalaciones válidas de {game_key} en Steam: " + "; ".join(str(p) for p in candidatas)
            ),
            candidates=candidatas,
        )
    unico = next(iter(unicos.values()))
    return ManagedSourceDiscoveryResult(state=DiscoveryState.FOUND, message="Managed Source descubierta", source=unico)

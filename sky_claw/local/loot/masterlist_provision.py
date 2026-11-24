"""PR-B — Sky-Claw provee el masterlist y el prelude de LOOT (``updateMasterlist = false``).

Con ``updateMasterlist`` en su default (``true``), cada ``--auto-sort`` descarga
prelude + masterlist ANTES de ordenar; un fallo de red termina en un modal y la
corrida desatendida en timeout. Con la clave apagada LOOT lee sólo los archivos
locales del data root, y si el masterlist falta lo ignora EN SILENCIO y ordena
sin metadatos. La política decidida por el propietario (2026-11-24) es que
Sky-Claw los provea desde el daemon (único proceso con egress, vía
``NetworkGateway``) y que el worker se niegue a lanzar LOOT sin ellos.

Evidencia upstream — ``loot/loot`` tag ``0.29.1`` (commit ``77f3ba98``):

* ``src/gui/state/loot_settings.cpp:841-842``: clave TOML top-level
  ``updateMasterlist`` (default ``true``, ``loot_settings.h``).
* ``src/gui/qt/main_window.cpp:1245-1256``: con la clave activa, el sort encola
  ``UpdatePreludeTask`` + ``UpdateMasterlistTask`` antes de ordenar.
* ``src/gui/state/game/game_settings.h:37``: ``DEFAULT_MASTERLIST_BRANCH = "v0.29"``;
  ``game_settings.cpp:106-114``: ``https://raw.githubusercontent.com/loot/<repo>/<rama>/masterlist.yaml``
  con ``<repo> = "skyrimse"`` para ``tes5se`` y ``tes5vr`` (``game_settings.cpp:88-89``).
* ``src/gui/state/loot_settings.cpp:823-824``: prelude por default
  ``https://raw.githubusercontent.com/loot/prelude/<rama>/prelude.yaml``.
* ``src/gui/state/game/game.cpp:109-111, 390-393``: masterlist en
  ``<data>/games/<folderName>/masterlist.yaml``; ``loot_paths.cpp:74-75``:
  prelude en ``<data>/prelude/prelude.yaml``.
* ``src/gui/state/game/game.cpp:945-962`` (``loadMetadata``): sin masterlist no
  hay error ni aviso: se ordena sin metadatos. Por eso la verificación previa al
  spawn (:func:`verify_masterlist_provisioned`) es fail-closed.

La rama sale de la versión ATESTIGUADA del binario
(:func:`~sky_claw.local.loot.binary_version.read_loot_binary_version`) por una
tabla congelada: una versión sin rama verificada falla cerrada, porque un
masterlist de otra rama puede no parsear con ese LOOT.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import os
import pathlib
import re
import secrets
import time
from collections.abc import Awaitable, Callable, Mapping
from types import MappingProxyType
from typing import TYPE_CHECKING, Final

import yaml  # type: ignore[import-untyped]  # pyyaml sin stubs en el venv (mismo criterio que pefile/pynvml)

if TYPE_CHECKING:
    from sky_claw.app.security.network_gateway import NetworkGateway

logger = logging.getLogger(__name__)

#: ``(MAJOR, MINOR)`` de LOOT → rama del masterlist/prelude verificada upstream.
#: Congelado por igualdad literal en tests: sumar una versión exige su evidencia.
LOOT_MASTERLIST_BRANCHES: Final[Mapping[tuple[int, int], str]] = MappingProxyType({(0, 29): "v0.29"})

#: Carpeta de juego de LOOT (``--game``) → repositorio del masterlist.
LOOT_MASTERLIST_REPOSITORIES: Final[Mapping[str, str]] = MappingProxyType(
    {"Skyrim Special Edition": "skyrimse", "Skyrim VR": "skyrimse"}
)

#: Antigüedad máxima de una copia antes de intentar refrescarla.
MASTERLIST_TTL_SECONDS: Final[int] = 24 * 60 * 60

#: Tope de bytes aceptados por archivo (el masterlist de SSE ronda los 2-3 MiB).
MAX_MASTERLIST_BYTES: Final[int] = 32 * 1024 * 1024

_VERSION: Final[re.Pattern[str]] = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")
_SUFIJO_ORIGEN: Final[str] = ".skyclaw-source"

#: Descarga una URL y devuelve el cuerpo (200). Lanza ante cualquier otro resultado.
MasterlistFetcher = Callable[[str], Awaitable[bytes]]


class LootMasterlistError(RuntimeError):
    """El masterlist o el prelude no están disponibles con garantías: LOOT NO se lanza."""


@dataclasses.dataclass(frozen=True, slots=True)
class MasterlistProvisionReport:
    """Qué hizo :func:`provision_masterlist` por archivo, y los avisos no fatales."""

    acciones: Mapping[str, str]
    avisos: tuple[str, ...] = ()


def masterlist_branch(loot_version: str) -> str:
    """Rama verificada para *loot_version* (``MAJOR.MINOR.PATCH``), o falla cerrado."""
    match = _VERSION.fullmatch(loot_version)
    if match is None:
        raise LootMasterlistError(f"Versión de LOOT mal formada: {loot_version!r} (se espera MAJOR.MINOR.PATCH).")
    clave = (int(match[1]), int(match[2]))
    try:
        return LOOT_MASTERLIST_BRANCHES[clave]
    except KeyError:
        raise LootMasterlistError(
            f"LOOT {loot_version} no tiene una rama de masterlist verificada por Sky-Claw "
            f"(soportadas: {sorted(LOOT_MASTERLIST_BRANCHES)}); no se ordena con metadatos de otra versión."
        ) from None


def _repositorio(game_folder: str) -> str:
    try:
        return LOOT_MASTERLIST_REPOSITORIES[game_folder]
    except KeyError:
        raise LootMasterlistError(f"Juego de LOOT sin repositorio de masterlist conocido: {game_folder!r}.") from None


def masterlist_path(loot_data_path: pathlib.Path, game_folder: str) -> pathlib.Path:
    """``<data>/games/<folder>/masterlist.yaml`` (``game.cpp:109-111, 390-393``)."""
    _repositorio(game_folder)
    return loot_data_path / "games" / game_folder / "masterlist.yaml"


def prelude_path(loot_data_path: pathlib.Path) -> pathlib.Path:
    """``<data>/prelude/prelude.yaml`` (``loot_paths.cpp:74-75``)."""
    return loot_data_path / "prelude" / "prelude.yaml"


def masterlist_url(game_folder: str, loot_version: str) -> str:
    return (
        f"https://raw.githubusercontent.com/loot/{_repositorio(game_folder)}/"
        f"{masterlist_branch(loot_version)}/masterlist.yaml"
    )


def prelude_url(loot_version: str) -> str:
    return f"https://raw.githubusercontent.com/loot/prelude/{masterlist_branch(loot_version)}/prelude.yaml"


def verify_masterlist_provisioned(loot_data_path: pathlib.Path, game_folder: str) -> None:
    """Exige masterlist y prelude no vacíos antes del spawn (LOOT los ignoraría en silencio)."""
    for ruta in (masterlist_path(loot_data_path, game_folder), prelude_path(loot_data_path)):
        try:
            tamano = ruta.stat().st_size
        except OSError as exc:
            raise LootMasterlistError(
                f"Falta {ruta} ({exc}): con updateMasterlist=false LOOT ordenaría sin metadatos. "
                "Sky-Claw lo descarga antes de cada sort; revisá la red y reintentá."
            ) from exc
        if tamano == 0:
            raise LootMasterlistError(f"{ruta} está vacío: LOOT ordenaría sin metadatos.")


def _origen(ruta: pathlib.Path) -> str | None:
    try:
        return ruta.with_name(ruta.name + _SUFIJO_ORIGEN).read_text(encoding="utf-8").strip()
    except OSError:
        return None


def _vigente(ruta: pathlib.Path, url: str, *, ttl: int, ahora: float) -> bool:
    try:
        estado = ruta.stat()
    except OSError:
        return False
    return estado.st_size > 0 and _origen(ruta) == url and ahora - estado.st_mtime < ttl


def _validar_yaml(contenido: bytes, url: str) -> None:
    if not contenido or len(contenido) > MAX_MASTERLIST_BYTES:
        raise LootMasterlistError(f"{url}: tamaño fuera de rango ({len(contenido)} bytes).")
    try:
        documento = yaml.safe_load(contenido.decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise LootMasterlistError(f"{url} no es YAML UTF-8 válido: {exc}") from exc
    if not isinstance(documento, dict):
        raise LootMasterlistError(f"{url} no tiene la forma de un masterlist/prelude de LOOT (mapping top-level).")


def _escribir_atomico(destino: pathlib.Path, contenido: bytes, url: str) -> None:
    destino.parent.mkdir(parents=True, exist_ok=True)
    for ruta, datos in ((destino, contenido), (destino.with_name(destino.name + _SUFIJO_ORIGEN), url.encode())):
        temporal = ruta.with_name(f"{ruta.name}.skyclaw-{secrets.token_hex(8)}.tmp")
        try:
            with temporal.open("xb") as handle:
                handle.write(datos)
            os.replace(temporal, ruta)
        except OSError:
            with contextlib.suppress(OSError):
                temporal.unlink()
            raise


async def provision_masterlist(
    loot_data_path: pathlib.Path,
    *,
    game_folder: str,
    loot_version: str,
    fetch: MasterlistFetcher | None,
    ttl: int = MASTERLIST_TTL_SECONDS,
    reloj: Callable[[], float] = time.time,
) -> MasterlistProvisionReport:
    """Deja masterlist y prelude de la rama de *loot_version* en el data root.

    Una copia de la MISMA fuente y dentro del TTL no se descarga. Si la descarga
    falla, una copia previa de la misma fuente se conserva con un aviso (LOOT
    hace lo mismo con su propio caché); sin copia válida, o con una copia de
    otra rama, falla cerrado. Sin *fetch* (superficie sin egress) sólo valida.

    Raises:
        LootMasterlistError: no queda un masterlist/prelude utilizable.
    """
    destinos = {
        "prelude": (prelude_path(loot_data_path), prelude_url(loot_version)),
        "masterlist": (masterlist_path(loot_data_path, game_folder), masterlist_url(game_folder, loot_version)),
    }
    acciones: dict[str, str] = {}
    avisos: list[str] = []
    ahora = reloj()
    for nombre, (ruta, url) in destinos.items():
        if await asyncio.to_thread(_vigente, ruta, url, ttl=ttl, ahora=ahora):
            acciones[nombre] = "vigente"
            continue
        misma_fuente = await asyncio.to_thread(_origen, ruta) == url and await asyncio.to_thread(ruta.is_file)
        try:
            if fetch is None:
                raise LootMasterlistError("esta superficie no tiene egress cableado para descargarlo")
            contenido = await fetch(url)
            await asyncio.to_thread(_validar_yaml, contenido, url)
            await asyncio.to_thread(_escribir_atomico, ruta, contenido, url)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — frontera de red: cualquier fallo decide por la copia previa
            if not misma_fuente:
                raise LootMasterlistError(
                    f"No hay {nombre} de LOOT utilizable para {url}: {exc}. Sin él LOOT ordenaría sin "
                    "metadatos; el sort no se ejecuta."
                ) from exc
            aviso = f"No se pudo refrescar el {nombre} ({exc}); se usa la copia anterior de {url}."
            logger.warning(aviso)
            avisos.append(aviso)
            acciones[nombre] = "copia_anterior"
            continue
        acciones[nombre] = "descargado"
    await asyncio.to_thread(verify_masterlist_provisioned, loot_data_path, game_folder)
    return MasterlistProvisionReport(acciones=MappingProxyType(acciones), avisos=tuple(avisos))


class GatewayMasterlistFetcher:
    """:data:`MasterlistFetcher` sobre el ``NetworkGateway`` (allowlist + SSRF del daemon)."""

    def __init__(self, gateway: NetworkGateway, *, timeout_seconds: float = 60.0) -> None:
        self._gateway = gateway
        self._timeout_seconds = timeout_seconds

    async def __call__(self, url: str) -> bytes:
        import aiohttp

        from sky_claw.app.security.network_gateway import GatewayTCPConnector

        timeout = aiohttp.ClientTimeout(total=self._timeout_seconds)
        # Mismo patrón que ScraperAgent: el connector del gateway fija el DNS (anti rebinding).
        connector = GatewayTCPConnector(self._gateway, limit=2)
        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
            resp = await self._gateway.request("GET", url, session)
            async with resp:
                if resp.status != 200:
                    raise LootMasterlistError(f"{url}: HTTP {resp.status}")
                contenido = await resp.content.read(MAX_MASTERLIST_BYTES + 1)
        if len(contenido) > MAX_MASTERLIST_BYTES:
            raise LootMasterlistError(f"{url}: supera {MAX_MASTERLIST_BYTES} bytes")
        return contenido

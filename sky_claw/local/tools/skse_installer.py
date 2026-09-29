"""Herramienta completa para descargar e instalar SKSE en Skyrim.

Contiene la implementación completa: detección del runtime, resolución exacta
del release, adquisición Silverlock/Nexus, aprobación HITL, límites de descarga,
extracción segura, revalidación, copia, limpieza y verificación.

Las dependencias genéricas de ``ToolsInstaller`` se reutilizan, pero la lógica
específica de SKSE vive en este archivo.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import os
import pathlib
import re
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, TypeVar

import aiohttp

from sky_claw.app.security.hitl import Decision, new_hitl_request_id
from sky_claw.app.security.network_gateway import EgressViolationError, NetworkGatewayTimeoutError
from sky_claw.local.discovery.environment import SkyrimEdition
from sky_claw.local.discovery.scanner import (
    detect_skyrim_edition,
    find_skse_installation,
    read_skyrim_version,
    skyrim_version_matches,
)
from sky_claw.local.thread_bridge import esperar_hilo_ininterrumpible as _esperar_hilo_ininterrumpible
from sky_claw.local.tools.skse_catalog import SkseRelease, SkseSource, resolve_skse_release
from sky_claw.local.tools_installer import (
    _DOWNLOAD_CHUNK_SIZE,
    InstallResult,
    InstallVerification,
    ToolInstallError,
    _bajo_lock_de_instalacion,
    _install_lock_resource_id,
)

if TYPE_CHECKING:
    from sky_claw.app.scraper.nexus_downloader import FileInfo, NexusDownloader
logger = logging.getLogger(__name__)
_T = TypeVar("_T")
_SKSE_MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
_SKYRIM_EXE_NAMES = ("SkyrimSE.exe", "SkyrimVR.exe", "Skyrim.exe")
_ES_WINDOWS = os.name == "nt"
_game_version_matches = skyrim_version_matches

SKSE_CONFIG: dict[str, dict[str, str | None]] = {
    "AE": {
        "url": "https://skse.silverlock.org/beta/skse64_2_02_06.7z",
        "sha256": "D7297F1A1D613E5265E1AF4DBBFE8BD37A32719C1CCEF363FC6187FA6EBA0848",
        "dll": "skse64_1_6_1170.dll",
        "loader": "skse64_loader.exe",
        "steam_loader": "skse64_steam_loader.dll",
    },
    "SE": {
        "url": "https://skse.silverlock.org/beta/skse64_2_00_20.7z",
        "sha256": "46F70B963B22E3C242BAC45E1716C39349798FBA5B74C978419A10D604B542D7",
        "dll": "skse64_1_5_97.dll",
        "loader": "skse64_loader.exe",
        "steam_loader": "skse64_steam_loader.dll",
    },
    "LE": {
        "url": "https://skse.silverlock.org/beta/skse_1_07_03.7z",
        "sha256": "1825024212A20A6A197BBCD308073B497E516139AD48D60685D5B18AB4BBE76D",
        "dll": "skse_1_9_32.dll",
        "loader": "skse_loader.exe",
        "steam_loader": None,  # LE no tiene steam_loader
    },
}


def _reject_oversized_skse_content_length(resp: Any) -> None:
    """Corta la descarga de SKSE si el origen ya declara un tamaño imposible.

    Es un atajo, no la defensa: un ``Content-Length`` ausente, malformado o mentiroso
    no habilita nada, porque el contador real de bytes se sigue evaluando por chunk.
    """
    raw = getattr(resp, "headers", {}) or {}
    declared = raw.get("Content-Length")
    if declared is None:
        return
    try:
        size = int(declared)
    except (TypeError, ValueError):
        return
    if size > _SKSE_MAX_ARCHIVE_BYTES:
        raise ToolInstallError(
            f"El origen declara un archive de SKSE de {size} bytes, que excede el tamaño "
            f"máximo permitido ({_SKSE_MAX_ARCHIVE_BYTES} bytes)."
        )


def _skse_cfg_field(cfg: dict[str, str | None], field: str) -> str:
    """Lee un campo obligatorio de un payload de ``SKSE_CONFIG``, fallando explícito si falta.

    No usa ``assert``: con ``python -O`` los asserts se compilan fuera y un payload
    incompleto sigue de largo hasta construir un ``install_dir / None`` (``TypeError``
    críptico a mitad de la instalación, después de la aprobación HITL) en vez de cortar
    con el contrato del método.
    """
    value = cfg.get(field)
    if not value:
        raise ToolInstallError(f"Payload de SKSE mal configurado: falta el campo obligatorio '{field}' en SKSE_CONFIG.")
    return value


@dataclass(frozen=True, slots=True)
class _SkseNexusAcquisition:
    """Canal de adquisición Nexus para los builds SKSE64 de Steam."""

    nexus_id: int
    display_name: str
    loader_name: str
    steam_loader_name: str | None


def _adquisicion_para(release: SkseRelease) -> dict[str, str | None] | _SkseNexusAcquisition:
    """Resuelve la adquisición para *release*: payload legacy silverlock o canal Nexus.

    La compatibilidad ya la decidió el catálogo (runtime → release); acá sólo se
    resuelve de dónde bajar ese build, por IDENTIDAD y nunca por edición:

    * SILVERLOCK → el payload legacy de ``SKSE_CONFIG`` que declare el MISMO ``dll``
      Y el MISMO archive. ``artifact_name`` es OBLIGATORIO en este canal: sin él la
      identidad quedaría en "sólo DLL" y podría matchear un build viejo de la misma
      familia (p. ej. el 2.2.6 de 1.6.1170). Si no hay EXACTAMENTE una coincidencia
      se corta antes del HITL, del egress y de cualquier escritura.
    * NEXUS → la spec única del canal (mod 30379, familia Steam/64). Si el release
      no pertenece a esa familia (p. ej. un futuro build LE publicado en Nexus) el
      canal no le aplica y se corta cerrado.
    """
    if release.source is not SkseSource.SILVERLOCK:
        if not release.dll_name.startswith("skse64_"):
            raise ToolInstallError(
                f"El canal Nexus de Sky-Claw sólo cubre la familia SKSE64; el release para "
                f"{release.game_version} ({release.dll_name}, vía {release.source.value}) no le "
                "aplica. No se descargó ni se modificó nada."
            )
        return _SKSE_NEXUS

    if not release.artifact_name:
        raise ToolInstallError(
            f"Release SILVERLOCK sin artifact_name verificado (SKSE {release.skse_version}, "
            f"runtime {release.game_version}): la identidad del payload exige dll + archive, "
            "así que no se selecciona por nombre de DLL. No se descargó ni se modificó nada."
        )

    candidatos = [
        cfg
        for cfg in SKSE_CONFIG.values()
        if cfg.get("dll") == release.dll_name and (cfg.get("url") or "").rsplit("/", 1)[-1] == release.artifact_name
    ]
    if len(candidatos) != 1:
        raise ToolInstallError(
            f"No hay una adquisición directa única para SKSE {release.skse_version} "
            f"(runtime {release.game_version}): SKSE_CONFIG declara {len(candidatos)} payload(s) "
            "coherentes con su identidad (dll/artifact). No se descargó ni se modificó nada."
        )
    return candidatos[0]


def _seleccionar_archivo_nexus(files: list[dict[str, Any]], release: SkseRelease) -> dict[str, Any]:
    """Elegir EL archivo Nexus del mod 30379 que implementa *release*.

    Identidad: ``name`` exacto (familia HKSE64 Steam; el GOG tiene nombre distinto),
    ``mod_version == release.skse_version`` y nunca DELETED. Duplicados legítimos
    (re-subidas del autor con la misma versión) se desempatan ÚNICAMENTE por
    ``is_primary == True`` en exactamente uno; cualquier otra ambigüedad corta
    cerrado, sin orden por timestamp (sería "latest" disfrazado).
    """
    candidatos = [
        f
        for f in files
        if f.get("name") == _SKSE_NEXUS.display_name
        and str(f.get("mod_version") or "") == release.skse_version
        and f.get("category_name") != "DELETED"
    ]
    if len(candidatos) == 1:
        return candidatos[0]
    if len(candidatos) > 1:
        primarios = [f for f in candidatos if bool(f.get("is_primary"))]
        if len(primarios) == 1:
            return primarios[0]
    raise ToolInstallError(
        f"Nexus (mod {_SKSE_NEXUS.nexus_id}) no tiene un archivo ÚNICO que corresponda a "
        f"SKSE {release.skse_version} para el runtime {release.game_version} "
        f"(candidatos coherentes: {len(candidatos)}). No se descargó ni se modificó nada."
    )


def _validar_artifact_skse_nexus(file_info: FileInfo) -> None:
    """Gates de integridad ANTES de bajar código binario directo al juego.

    La validación de hash/tamaño de `NexusDownloader.download` es necesaria pero no
    suficiente acá: si Nexus no entregó md5 el downloader continuaría con warning, y
    ese camino CORE escribe EXE/DLL en la raíz del juego. Para SKSE el md5 y el tamaño
    son OBLIGATORIOS y el tope es el mismo del canal silverlock (64 MiB, no 4 GiB).
    """
    md5 = (file_info.md5 or "").strip()
    if not _MD5_HEX_RE.fullmatch(md5):
        raise ToolInstallError(
            f"Nexus no publicó un MD5 válido para el artifact SKSE ({md5!r}): code nativo "
            "directo al juego exige hash verificable. No se descargó nada."
        )
    if file_info.size_bytes <= 0:
        raise ToolInstallError(
            "Nexus no informó el tamaño del artifact SKSE: sin límite comprobable no se descarga nada al juego."
        )
    if file_info.size_bytes > _SKSE_MAX_ARCHIVE_BYTES:
        raise ToolInstallError(
            f"El artifact SKSE ({file_info.size_bytes} bytes) supera el límite permitido "
            f"({_SKSE_MAX_ARCHIVE_BYTES} bytes). No se descargó nada."
        )
    if not file_info.file_name.lower().endswith(".7z"):
        raise ToolInstallError(
            f"El artifact SKSE de Nexus no es un .7z ({file_info.file_name!r}): sin formato "
            "verificable no se extrae. No se descargó nada."
        )


@dataclass(frozen=True, slots=True)
class _SkseNexusAcquisition:
    """Canal de adquisición Nexus para los builds SKSE64 de Steam.

    Los releases Nexus del catálogo (1.6.1170→2.2.8, 1.7.99→2.3.0, 1.7.104→2.3.1) son
    TODOS de la misma familia SKSE64 y viven en el mismo mod de Nexus — una sola spec,
    no una tabla paralela por runtime. La identidad del archivo dentro del mod se
    re-deriva en cada llamada desde la lista de la API por ``name + mod_version``
    con desempate estricto por ``is_primary``: sin `file_id` hardcodeado (un upload
    reemplazado cambiaría el id sin alterar su identidad semántica) y nunca por
    timestamp/vigencia (eso volvería a ser "latest" con pasos extra).
    """

    nexus_id: int
    display_name: str
    loader_name: str
    steam_loader_name: str | None


#: Perfil verificado el 2026-09-21 contra la pestaña Files del mod: el archivo de la
#: familia Steam tiene exactamente este nombre y el GOG — deliberadamente fuera de
#: alcance — lleva otro ("...(SKSE64) GOG"). Los tres builds Nexus del catálogo
#: (2.2.8/2.3.0/2.3.1) cuelgan todos de ahí.
_SKSE_NEXUS = _SkseNexusAcquisition(
    nexus_id=30379,
    display_name="Skyrim Script Extender (SKSE64) Steam",
    loader_name="skse64_loader.exe",
    steam_loader_name="skse64_steam_loader.dll",
)

#: Formato MD5 hexadecimal (32 dígitos). Exigible para SKSE: lo que se baja va
#: directo a EXE/DLL del juego y la validación de `NexusDownloader` NO puede ser
#: opcional en este camino (si Nexus no publica hash, cortamos — no hay archive).
_MD5_HEX_RE = re.compile(r"[0-9a-f]{32}", re.IGNORECASE)


def _adquisicion_para(release: SkseRelease) -> dict[str, str | None] | _SkseNexusAcquisition:
    """Resuelve la adquisición para *release*: payload legacy silverlock o canal Nexus.

    La compatibilidad ya la decidió el catálogo (runtime → release); acá sólo se
    resuelve de dónde bajar ese build, por IDENTIDAD y nunca por edición:

    * SILVERLOCK → el payload legacy de ``SKSE_CONFIG`` que declare el MISMO ``dll``
      Y el MISMO archive. ``artifact_name`` es OBLIGATORIO en este canal: sin él la
      identidad quedaría en "sólo DLL" y podría matchear un build viejo de la misma
      familia (p. ej. el 2.2.6 de 1.6.1170). Si no hay EXACTAMENTE una coincidencia
      se corta antes del HITL, del egress y de cualquier escritura.
    * NEXUS → la spec única del canal (mod 30379, familia Steam/64). Si el release
      no pertenece a esa familia (p. ej. un futuro build LE publicado en Nexus) el
      canal no le aplica y se corta cerrado.
    """
    if release.source is not SkseSource.SILVERLOCK:
        if not release.dll_name.startswith("skse64_"):
            raise ToolInstallError(
                f"El canal Nexus de Sky-Claw sólo cubre la familia SKSE64; el release para "
                f"{release.game_version} ({release.dll_name}, vía {release.source.value}) no le "
                "aplica. No se descargó ni se modificó nada."
            )
        return _SKSE_NEXUS

    if not release.artifact_name:
        raise ToolInstallError(
            f"Release SILVERLOCK sin artifact_name verificado (SKSE {release.skse_version}, "
            f"runtime {release.game_version}): la identidad del payload exige dll + archive, "
            "así que no se selecciona por nombre de DLL. No se descargó ni se modificó nada."
        )

    candidatos = [
        cfg
        for cfg in SKSE_CONFIG.values()
        if cfg.get("dll") == release.dll_name and (cfg.get("url") or "").rsplit("/", 1)[-1] == release.artifact_name
    ]
    if len(candidatos) != 1:
        raise ToolInstallError(
            f"No hay una adquisición directa única para SKSE {release.skse_version} "
            f"(runtime {release.game_version}): SKSE_CONFIG declara {len(candidatos)} payload(s) "
            "coherentes con su identidad (dll/artifact). No se descargó ni se modificó nada."
        )
    return candidatos[0]


def _seleccionar_archivo_nexus(files: list[dict[str, Any]], release: SkseRelease) -> dict[str, Any]:
    """Elegir EL archivo Nexus del mod 30379 que implementa *release*.

    Identidad: ``name`` exacto (familia HKSE64 Steam; el GOG tiene nombre distinto),
    ``mod_version == release.skse_version`` y nunca DELETED. Duplicados legítimos
    (re-subidas del autor con la misma versión) se desempatan ÚNICAMENTE por
    ``is_primary == True`` en exactamente uno; cualquier otra ambigüedad corta
    cerrado, sin orden por timestamp (sería "latest" disfrazado).
    """
    candidatos = [
        f
        for f in files
        if f.get("name") == _SKSE_NEXUS.display_name
        and str(f.get("mod_version") or "") == release.skse_version
        and f.get("category_name") != "DELETED"
    ]
    if len(candidatos) == 1:
        return candidatos[0]
    if len(candidatos) > 1:
        primarios = [f for f in candidatos if bool(f.get("is_primary"))]
        if len(primarios) == 1:
            return primarios[0]
    raise ToolInstallError(
        f"Nexus (mod {_SKSE_NEXUS.nexus_id}) no tiene un archivo ÚNICO que corresponda a "
        f"SKSE {release.skse_version} para el runtime {release.game_version} "
        f"(candidatos coherentes: {len(candidatos)}). No se descargó ni se modificó nada."
    )


def _validar_artifact_skse_nexus(file_info: FileInfo) -> None:
    """Gates de integridad ANTES de bajar código binario directo al juego.

    La validación de hash/tamaño de `NexusDownloader.download` es necesaria pero no
    suficiente acá: si Nexus no entregó md5 el downloader continuaría con warning, y
    ese camino CORE escribe EXE/DLL en la raíz del juego. Para SKSE el md5 y el tamaño
    son OBLIGATORIOS y el tope es el mismo del canal silverlock (64 MiB, no 4 GiB).
    """
    md5 = (file_info.md5 or "").strip()
    if not _MD5_HEX_RE.fullmatch(md5):
        raise ToolInstallError(
            f"Nexus no publicó un MD5 válido para el artifact SKSE ({md5!r}): code nativo "
            "directo al juego exige hash verificable. No se descargó nada."
        )
    if file_info.size_bytes <= 0:
        raise ToolInstallError(
            "Nexus no informó el tamaño del artifact SKSE: sin límite comprobable no se descarga nada al juego."
        )
    if file_info.size_bytes > _SKSE_MAX_ARCHIVE_BYTES:
        raise ToolInstallError(
            f"El artifact SKSE ({file_info.size_bytes} bytes) supera el límite permitido "
            f"({_SKSE_MAX_ARCHIVE_BYTES} bytes). No se descargó nada."
        )
    if not file_info.file_name.lower().endswith(".7z"):
        raise ToolInstallError(
            f"El artifact SKSE de Nexus no es un .7z ({file_info.file_name!r}): sin formato "
            "verificable no se extrae. No se descargó nada."
        )


class SkseInstaller:
    """Implementación completa de la herramienta de instalación de SKSE."""

    def __init__(self, tools_installer: Any) -> None:
        self._tools_installer = tools_installer

    def __getattribute__(self, name: str) -> Any:
        if name not in {"_tools_installer", "__dict__", "__class__", "__getattribute__", "__getattr__"}:
            owner = object.__getattribute__(self, "_tools_installer")
            patched = getattr(owner, "__dict__", {}).get(name)
            if patched is not None:
                return patched
        return object.__getattribute__(self, name)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._tools_installer, name)

    async def ensure_skse(
        self,
        install_dir: pathlib.Path,
        session: aiohttp.ClientSession,
        edition: SkyrimEdition | None = None,
    ) -> InstallResult:
        """Asegura que SKSE esté disponible en el directorio del juego, descargándolo si es necesario.

        Args:
            install_dir: Directorio raíz del juego Skyrim (donde reside SkyrimSE.exe).
            session: Sesión HTTP activa.
            edition: Hint LEGACY de clasificación. NO selecciona el release y, cuando
                hay ejecutable, tampoco la familia: la clasificación real del PE manda
                (un ``SkyrimVR.exe`` corta por veto de producto, y la idempotencia
                busca la familia real del ejecutable, no la pedida). Se conserva por
                compatibilidad de firma; sin runtime legible la operación corta
                cerrada de todos modos.

        Returns:
            :class:`InstallResult` con la ruta al loader de SKSE.

        Raises:
            ToolInstallError: Si la instalación falla, el runtime no tiene release
                conocido o la adquisición del release no está habilitada.
        """
        self._validator.validate(install_dir)
        # Lock cross-process keyed por game_dir (8º mutador de T-31). Cubre el
        # ciclo completo: chequeo → descarga → copia → limpieza de DLL huérfanos.
        async with _bajo_lock_de_instalacion(
            self._lock_manager,
            _install_lock_resource_id(install_dir),
            ttl=self._install_ttl,
        ):
            # 1) RUNTIME PRIMERO. Quién nombra la EDICIÓN y quién prueba la VERSIÓN
            # son cosas distintas: la compatibilidad de SKSE es del BUILD exacto del
            # ejecutable, no de la edición — `1.6.1170`, `1.7.99` y `1.7.104` son
            # todos "AE" y cada uno exige un build distinto. Los dos caminos llenan
            # `detected_version` y `hay_ejecutable`; la edición —SIEMPRE la del
            # ejecutable real cuando existe— queda para los mensajes, el veto de
            # producto y la FAMILIA de loader que el scanner espera, nunca para
            # elegir el build.
            detected_version = ""
            if edition is None:
                edition, detected_version = await self._detect_skyrim_edition_from_exe(install_dir)
                # `_detect_skyrim_edition_from_exe` levanta si no encuentra ninguno.
                hay_ejecutable = True
            else:
                # El hint del caller NO es autoridad de familia cuando hay un
                # ejecutable: la clasificación REAL del PE manda. Un `SkyrimVR.exe`
                # cuya versión "parezca" soportada tiene que cortar por veto (abajo)
                # en vez de resolver catálogo y bajar SKSE64 al directorio de un VR;
                # y la idempotencia tiene que buscar la familia REAL — un
                # `edition=LE` sobre un Skyrim SE con el SKSE correcto ya puesto no
                # puede "no encontrarlo" y reinstalar encima. Sin ejecutable no hay
                # runtime, así que el flujo corta cerrado más abajo.
                exe = self._encontrar_ejecutable(install_dir)
                hay_ejecutable = exe is not None
                if exe is not None:
                    detected_version = await self._leer_pe_tolerando_ilegible(read_skyrim_version, exe, ilegible="")
                    edition = await self._leer_pe_tolerando_ilegible(
                        detect_skyrim_edition, exe, ilegible=SkyrimEdition.UNKNOWN
                    )

            # 2) VETO DE PRODUCTO POR EDICIÓN (VR / MS Store). Es lo único que la
            # edición sigue decidiendo, y es un "no": se evalúa sobre la clasificación
            # REAL del PE — nunca sobre el hint del caller — y `UNKNOWN` no sale de
            # `SkyrimSE.exe`/`Skyrim.exe` (esos nombres resuelven siempre a SE/AE/LE),
            # así que acá caen `SkyrimVR.exe` y cualquier binario exótico. Sin
            # ejecutable no aplica: ese camino corta en el paso 3.
            if edition is SkyrimEdition.UNKNOWN:
                raise ToolInstallError(
                    f"La edición {edition.value} no es compatible con SKSE (MS Store y VR quedan "
                    "fuera del autoinstalador). No se descargó ni se modificó nada."
                )

            # 3) SIN RUNTIME LEGIBLE NO HAY RELEASE QUE ELEGIR: fail-closed ANTES del
            # HITL, del egress y de cualquier escritura. Degradar "a edición sola" era
            # fail-open: `read_skyrim_version` devuelve "" en tres casos reales (sin
            # `pefile`, PE que no parsea, PE sin recurso de versión) y en esa MISMA
            # rama la edición no sale del PE sino de una heurística de TAMAÑO de
            # archivo (`scanner._detect_skyrim_version`: >60 MB ⇒ AE), así que se
            # instalaba el payload de un build adivinado sobre un runtime desconocido.
            #
            # PRESENCIA != COMPATIBILIDAD: si hay ejecutable y una instalación física
            # reconocible (loader + algún DLL de runtime — la MISMA detección del
            # scanner, importada y no duplicada), se reporta PRESENT_BUT_UNVERIFIED sin
            # tocar nada. Ese camino no descarga ni escribe, así que negarle el no-op a
            # una máquina cuyo PE no se puede leer rompería una instalación que
            # funciona sin ganar ninguna garantía (misma política que
            # `scanner.find_skse_installation`, que ante versión vacía degrada a
            # presencia). Sin instalación, o sin ejecutable, el error es accionable
            # ANTES de cualquier frontera.
            #
            # El staging con `edition` explícita y sin ejecutable quedó ELIMINADO
            # (PR-2): sin runtime exacto no hay SkseRelease posible, y la arquitectura
            # runtime-first no puede volver a edition-first por una puerta lateral. El
            # único caller productivo (GUI → `run_ritual_install`) pasa la carpeta del
            # snapshot del scanner y no usaba ese camino.
            if not detected_version:
                if hay_ejecutable:
                    loader_presente = find_skse_installation(
                        install_dir, edition=SkyrimEdition.UNKNOWN, game_version=""
                    )
                    if loader_presente is not None:
                        logger.info("SKSE presente sin verificar en %s", loader_presente)
                        return InstallResult(
                            tool_name="SKSE",
                            exe_path=loader_presente,
                            version="existing",
                            already_existed=True,
                            verification=InstallVerification.PRESENT_BUT_UNVERIFIED,
                        )
                    raise ToolInstallError(
                        f"No pude leer la versión exacta de tu Skyrim en {install_dir} "
                        "(¿falta `pefile`, o el ejecutable no expone su recurso de versión?). "
                        "SKSE está pinneado a la versión EXACTA del ejecutable, así que sin poder "
                        "probar la compatibilidad no se instala nada: elegir el build por edición "
                        "sola escribe un DLL que puede no cargar. Instalá manualmente el build "
                        "correspondiente desde https://skse.silverlock.org/."
                    )
                raise ToolInstallError(
                    f"No encontré el ejecutable de Skyrim en {install_dir}: sin el runtime exacto "
                    "no puedo elegir un release de SKSE (la compatibilidad se decide por el build "
                    "del ejecutable, no por la edición). No se descargó ni se modificó nada. "
                    "Revisá skyrim_path o instalá el build correspondiente a mano desde "
                    "https://skse.silverlock.org/."
                )

            # 4) CATÁLOGO: runtime exacto → release exacto. Un runtime que el catálogo
            # no conoce NO cae al "último conocido", a la edición ni al payload AE:
            # SKSE está pinneado al build del ejecutable y un DLL de otro runtime no
            # carga. Corre antes del HITL, del egress y de cualquier escritura.
            release = resolve_skse_release(detected_version)
            if release is None:
                raise ToolInstallError(
                    f"Sky-Claw no tiene un release de SKSE conocido para el runtime "
                    f"{detected_version} de tu Skyrim: SKSE está pinneado al build EXACTO del "
                    "ejecutable, así que no se instala el build de otro runtime ni se adivina "
                    "'el más nuevo'. No se descargó ni se modificó nada. Si existe un build para "
                    "tu versión, se puede instalar a mano desde https://skse.silverlock.org/."
                )

            # 5) IDEMPOTENCIA: la autoridad es la MISMA detección que usa el scanner
            # (`find_skse_installation`) — loader del juego + algún DLL de runtime que
            # corresponda al runtime REAL detectado. Reemplaza al par loader+DLL del
            # payload por edición, que daba un mismatch espurio: con Skyrim 1.7.104 y
            # SKSE 2.3.1 instalado a mano (`skse64_1_7_104.dll`), el contrato viejo
            # buscaba el 1.6.1170 de `SKSE_CONFIG["AE"]`, no lo encontraba y mandaba a
            # instalar algo que ya estaba. El build concreto (p. ej. 2.2.6 vs 2.2.8)
            # no cambia el desenlace: el nombre del DLL codifica el RUNTIME, no el
            # build de SKSE.
            loader_existente = find_skse_installation(install_dir, edition=edition, game_version=detected_version)
            if loader_existente is not None:
                logger.info(
                    "SKSE ya instalado en %s (runtime %s → SKSE %s)",
                    loader_existente,
                    detected_version,
                    release.skse_version,
                )
                return InstallResult(
                    tool_name="SKSE",
                    exe_path=loader_existente,
                    version="existing",
                    already_existed=True,
                    verification=InstallVerification.VERIFIED,
                )

            # 6) ADQUISICIÓN, todavía sin cruzar fronteras: si el release no tiene una
            # adquisición cableada —payload silverlock por identidad exacta o el canal
            # Nexus único— o la configuración local lo impide (sin API key de Nexus),
            # se corta acá: antes del HITL, de la red y de escribir. Sin este orden,
            # el operador pagaría una aprobación y una descarga inútiles.
            adq = _adquisicion_para(release)

            adquisidor: NexusDownloader | None = None
            if isinstance(adq, _SkseNexusAcquisition):
                factory = self._nexus_downloader_factory
                adquisidor = factory() if factory is not None else None
                if adquisidor is None:
                    raise ToolInstallError(
                        f"Sky-Claw reconoce que Skyrim {release.game_version} requiere SKSE "
                        f"{release.skse_version}, que sólo existe en Nexus Mods (mod "
                        f"{adq.nexus_id}). Para habilitar la descarga automática configurá la "
                        "API key de Nexus en Sky-Claw, o instalá el build correspondiente a mano "
                        "desde https://www.nexusmods.com/skyrimspecialedition/mods/"
                        f"{adq.nexus_id}. No se descargó ni se modificó nada."
                    )
                payload_cfg: dict[str, str | None] = {
                    "loader": adq.loader_name,
                    "dll": release.dll_name,
                    "steam_loader": adq.steam_loader_name,
                }
                url = f"https://www.nexusmods.com/skyrimspecialedition/mods/{adq.nexus_id}"
                fuente = "Nexus Mods (mod 30379)"
            else:
                payload_cfg = adq
                url = _skse_cfg_field(adq, "url")
                fuente = "skse.silverlock.org (sitio oficial)"

            loader_name = _skse_cfg_field(payload_cfg, "loader")
            dll_name = _skse_cfg_field(payload_cfg, "dll")
            loader_path = install_dir / loader_name

            # Solicitar aprobación HITL
            decision = await self._hitl.request_approval(
                request_id=new_hitl_request_id("skse-install"),
                reason=f"Install SKSE {release.skse_version} for Skyrim {detected_version}?",
                url=url,
                detail=(
                    f"URL: {url}\nLoader: {loader_name}\nDLL: {dll_name}\n"
                    f"Runtime: {release.game_version} → SKSE {release.skse_version}\n"
                    f"Source: {fuente}"
                ),
                category="download",
            )

            if decision is not Decision.APPROVED:
                raise ToolInstallError(f"SKSE installation denied by operator (decision={decision.value})")

            # Staging: la MISMA raíz que `AppContext` registra en el PathValidator
            # (`tempfile.gettempdir() / "sky_claw"`). Inventar otra —p. ej. C:/tmp— hace
            # que el `validate(tmp_path)` de acá abajo falle siempre, porque esa ruta no
            # es ninguna de las raíces del sandbox.
            skse_sandbox = pathlib.Path(tempfile.gettempdir()) / "sky_claw"
            skse_sandbox.mkdir(parents=True, exist_ok=True)

            with tempfile.TemporaryDirectory(dir=skse_sandbox) as tmpdir:
                tmp_path = pathlib.Path(tmpdir)
                self._validator.validate(tmp_path)

                extract_path = tmp_path / "extracted"

                if isinstance(adq, _SkseNexusAcquisition):
                    assert adquisidor is not None  # cortado más arriba
                    # La identidad del archivo se re-deriva SIEMPRE desde la API en
                    # esta corrida (no hay file_id hardcodeado): que el autor re-subiera
                    # un build no nos hace descargar otra cosa sin darnos cuenta.
                    try:
                        files = await adquisidor.list_files(adq.nexus_id, session)
                        seleccionado = _seleccionar_archivo_nexus(files, release)
                        file_info = await adquisidor.get_file_info(adq.nexus_id, int(seleccionado["file_id"]), session)
                    except ToolInstallError:
                        raise
                    except Exception as exc:
                        raise ToolInstallError(
                            f"No pude obtener la metadata de SKSE {release.skse_version} en "
                            f"Nexus (mod {adq.nexus_id}): {exc}"
                        ) from exc
                    # Gates de integridad antes de bajar código nativo al juego: MD5
                    # obligatorio y tamaño acotado (el downloader general llega a 4 GiB;
                    # para SKSE sigue valiendo el tope de 64 MiB del canal silverlock).
                    _validar_artifact_skse_nexus(file_info)
                    archive_path = await adquisidor.download(file_info, session)
                    try:
                        await _esperar_hilo_ininterrumpible(self._extract, archive_path, extract_path)
                    finally:
                        # El archive está en staging del downloader y NECESITA limpieza
                        # incluso si la extracción falla: si no, un .7z corrupto quedaría
                        # como "descarga exitosa cacheada" para el próximo intento.
                        with contextlib.suppress(OSError):
                            archive_path.unlink(missing_ok=True)
                else:
                    archive_name = url.split("/")[-1]
                    archive_path = tmp_path / archive_name
                    self._validator.validate(archive_path)

                    # Descarga segura vía NetworkGateway con timeout
                    await self._download_skse_archive(session, adq, archive_path)

                    # Extraer con protección zip-slip
                    await _esperar_hilo_ininterrumpible(self._extract, archive_path, extract_path)

                # Buscar loader excluyendo __MACOSX
                skse_root = self._find_skse_root(extract_path, payload_cfg)

                # SEGUNDO GATE, pegado a la primera escritura. El de arriba probó la
                # compatibilidad al ARRANCAR la operación; desde entonces pasaron tres
                # cosas que duran —la espera del HITL, la descarga y la extracción— y
                # Skyrim es un ejecutable que otro proceso (Steam) actualiza sin
                # avisarnos. Sin releer, lo que quedaba demostrado era "el runtime ERA
                # compatible cuando empecé", y lo que decide si el DLL carga es si lo
                # SIGUE siendo cuando escribo.
                #
                # Va acá y no antes de la descarga: cuanto más lejos de la copia, más
                # ventana queda sin cubrir. Y va antes de `_copy_skse_files` porque esa
                # es la primera mutación del directorio del juego — todo lo anterior
                # (sandbox, archive, extracción) vive en staging temporal.
                #
                # Compara contra `release.game_version` (el runtime que el payload
                # descargado targetea), no contra la edición ni contra el cfg de
                # adquisición: la edición no identifica un build y el cfg, a esta
                # altura, sólo aporta de dónde bajar el archive.
                await self._revalidar_runtime_antes_de_mutar(install_dir, release.game_version)

                # Copiar archivos al directorio del juego. `_copy_skse_files` solo toca
                # nombres presentes en el payload nuevo (loader/dll de esta edición +
                # Data) — no depende de que los DLL huérfanos ya estén borrados.
                await self._copy_skse_files(skse_root, install_dir, payload_cfg)

                # LIMPIEZA DE DIRTY UPGRADES: recién acá, después de que la copia haya
                # terminado sin excepción. Es el verdadero punto de no retorno: si se
                # borrara antes y `_copy_skse_files` fallara a mitad de camino (disco
                # lleno, permisos), el juego se queda sin la versión vieja Y sin la
                # nueva completa, sin rollback que lo recupere. Corriendo al final, un
                # fallo de copia deja los DLL de otra edición intactos y el juego sigue
                # arrancando con el SKSE que tenía antes.
                await self._cleanup_orphaned_skse_dlls(install_dir, payload_cfg)

            logger.info("SKSE %s instalado en %s", release.skse_version, loader_path)
            # Explícito, NO heredado del default del dataclass. Detrás de este return
            # está la cadena entera: runtime legible que resolvió a un release del
            # catálogo, fuente de adquisición con identidad coherente, y la
            # revalidación pegada a la copia, que acaba de releer el PE del disco
            # contra `release.game_version`. Sin esa cadena este camino no existe (no
            # hay instalación sin runtime), así que `VERIFIED` está probado, no
            # heredado.
            #
            # `version` sale del CATÁLOGO (`release.skse_version`), no del stem de la
            # URL del payload: es la identidad semántica del build ("2.0.20") y ningún
            # caller dependía del formato viejo (la GUI usa `exe_path`, `verification`
            # y `already_existed`; el único consumidor de `version` es la superficie
            # del agente LLM, donde SKSE no está).
            return InstallResult(
                tool_name="SKSE",
                exe_path=loader_path,
                version=release.skse_version,
                already_existed=False,
                verification=InstallVerification.VERIFIED,
            )

    async def _revalidar_runtime_antes_de_mutar(
        self,
        game_dir: pathlib.Path,
        game_version: str,
    ) -> None:
        """Vuelve a probar la compatibilidad contra el ejecutable REAL, justo antes de escribir.

        No confía en nada de la primera pasada: relee el PE del disco. Ni la versión
        detectada al arrancar, ni la edición, ni el nombre del DLL elegido sirven acá
        — todos son de antes de la ventana, y la ventana es justamente el problema.

        ``game_version`` es el runtime que el payload descargado targetea
        (``SkseRelease.game_version``): el ÚNICO valor de compatibilidad que este
        gate conoce, elegido por el catálogo al arrancar — nunca por edición ni por
        el cfg de adquisición.

        Falla cerrado en las tres formas de no poder probar: sin ejecutable, sin
        versión legible, o versión que no matchea el payload que se bajó. La rama
        "nunca hubo ejecutable" (staging con ``edition`` explícita) ya no existe:
        sin runtime no se elige release y el flujo no llega hasta acá, así que un
        ejecutable ausente AHORA es siempre un cambio de estado (el juego se
        desinstaló o se movió durante la operación).
        """
        hay_ejecutable, version_actual = await self._leer_version_del_ejecutable(game_dir)

        if not hay_ejecutable:
            raise ToolInstallError(
                f"El payload de SKSE para el runtime {game_version} ya se descargó, pero ya no "
                f"encuentro el ejecutable de Skyrim en {game_dir}: desapareció mientras se "
                "preparaba la instalación (¿se desinstaló o se movió el juego?). No se copió nada."
            )

        if not version_actual:
            raise ToolInstallError(
                f"La compatibilidad de tu Skyrim en {game_dir} dejó de poder verificarse "
                "mientras se preparaba la instalación: el ejecutable está pero ya no expone su "
                "versión (¿una actualización en curso?). No se copió nada; reintentá cuando el "
                "juego esté en reposo."
            )

        if not _game_version_matches(version_actual, game_version):
            raise ToolInstallError(
                f"Tu Skyrim cambió de versión mientras se preparaba la instalación: ahora está "
                f"en {version_actual} y el payload que se descargó es para {game_version}. "
                "Copiarlo dejaría un SKSE que no carga, así que no se copió nada. Volvé a "
                f"intentarlo para que se elija el build correspondiente a {version_actual}."
            )

    async def _leer_version_del_ejecutable(self, game_dir: pathlib.Path) -> tuple[bool, str]:
        """``(¿hay ejecutable de Skyrim?, versión exacta o "")`` sin exigir que exista.

        Hermano de :meth:`_detect_skyrim_edition_from_exe` para quien sólo necesita
        saber si hay un runtime real cuya compatibilidad se pueda probar (la
        relectura del segundo gate, por ejemplo). Los dos desenlaces que devuelve
        ``""`` son distintos y por eso se devuelve también el booleano: "no hay
        Skyrim acá" (sin runtime exacto no se elige release, y el caller corta
        cerrado) y "hay uno y no pude leerle la versión" (presencia sin verificar o
        corte accionable).
        """
        exe = self._encontrar_ejecutable(game_dir)
        if exe is None:
            return False, ""
        return True, await self._leer_pe_tolerando_ilegible(read_skyrim_version, exe, ilegible="")

    def _encontrar_ejecutable(self, game_dir: pathlib.Path) -> pathlib.Path | None:
        """Primer ejecutable de Skyrim presente en *game_dir*, en el orden canónico.

        Un solo lugar para el orden (``_SKYRIM_EXE_NAMES``): antes lo repetían —y
        podían hacerlo divergir— la autodetección y la lectura de versión. Importa
        cuando conviven varios (un dir con ``SkyrimSE.exe`` y ``SkyrimVR.exe``):
        gana el SE, igual que antes.
        """
        for exe_name in _SKYRIM_EXE_NAMES:
            exe = game_dir / exe_name
            if exe.is_file():
                return exe
        return None

    async def _leer_pe_tolerando_ilegible(
        self,
        lector: Callable[[pathlib.Path], _T],
        exe: pathlib.Path,
        *,
        ilegible: _T,
    ) -> _T:
        """Corre un lector de PE fuera del event loop y traduce "no se pudo leer".

        Punto ÚNICO de traducción, compartido por los dos caminos que leen el PE
        —autodetección de edición y relectura de versión—. Estuvo primero solo en uno
        de los dos y el revisor encontró el gemelo intacto: con la traducción repetida
        en cada sitio, el tercero que lea un PE vuelve a olvidarla.
        """
        try:
            # pefile hace I/O síncrono de PE: fuera del event loop.
            return await asyncio.to_thread(lector, exe)
        except Exception as exc:
            # Se traduce SOLO el `Exception` pelado, que es exactamente lo que
            # `pefile` levanta en al menos un caso de acceso (ruta que resulta ser un
            # directorio) y que queda fuera de las tres ramas que
            # `_read_pe_product_version` captura. El `is_file()` del caller no alcanza:
            # entre esa comprobación y la lectura la ruta puede cambiar, y esto corre
            # precisamente cuando el juego se está actualizando.
            #
            # Cualquier SUBTIPO se re-lanza: un `TypeError` por cambio de firma o un
            # `AttributeError` por una versión nueva de `pefile` son defectos NUESTROS,
            # y traducirlos a "ilegible" le diría al operador que su Skyrim no se puede
            # verificar cuando el problema es de Sky-Claw — con el traceback real
            # enterrado en un warning y sin forma de distinguir una cosa de la otra.
            #
            # BLE001 está exento para este archivo en `pyproject.toml`, así que ruff NO
            # habría marcado el catch-all: el recorte por tipo exacto está porque el
            # gate no cubre este código, no porque lo cubra.
            if type(exc) is not Exception:
                raise
            logger.warning(
                "No se pudo leer el PE de %s con %s (%s: %s); se trata como ilegible.",
                exe,
                getattr(lector, "__name__", lector),
                type(exc).__name__,
                exc,
            )
            return ilegible

    async def _detect_skyrim_edition_from_exe(self, game_dir: pathlib.Path) -> tuple[SkyrimEdition, str]:
        """Deriva edición y versión exacta leyendo el PE del ejecutable en *game_dir*.

        Devuelve ambos: la compatibilidad de SKSE la decide el runtime EXACTO, así
        que el caller necesita la versión para resolver el release del catálogo. La
        edición queda para los mensajes, para el veto de producto (VR/MS Store/
        desconocido) y para elegir la FAMILIA de loader que el scanner espera.
        """
        exe = self._encontrar_ejecutable(game_dir)
        if exe is None:
            raise ToolInstallError(
                f"No encontré el ejecutable de Skyrim en {game_dir}: la compatibilidad de SKSE se "
                "decide por el runtime exacto del ejecutable, así que sin él no hay release que "
                "elegir. Revisá skyrim_path."
            )
        # Las dos lecturas van por el mismo traductor que la relectura del segundo
        # gate: un PE que explota acá tiene que dar el mismo `ToolInstallError`
        # accionable, no una excepción cruda por venir del camino de autodetección.
        # Edición ilegible -> UNKNOWN, que el veto de producto de `ensure_skse` corta
        # con su propio mensaje.
        edition = await self._leer_pe_tolerando_ilegible(detect_skyrim_edition, exe, ilegible=SkyrimEdition.UNKNOWN)
        version = await self._leer_pe_tolerando_ilegible(read_skyrim_version, exe, ilegible="")
        return edition, version

    async def _cleanup_orphaned_skse_dlls(
        self,
        game_dir: pathlib.Path,
        current_cfg: dict[str, str | None],
    ) -> None:
        """Elimina archivos DLL huérfanos de SKSE de versiones previas (limpieza de dirty upgrades)."""
        import stat

        # El glob de abajo es `skse*.dll`, que también matchea los steam loaders de
        # AMBAS familias. El de LE (`skse_steam_loader.dll`) no se deducía de
        # `current_cfg` porque LE tiene `steam_loader=None`, así que se lo nombra
        # explícito: borrarlo deja la instalación de LE sin loader de Steam.
        protected_names = {
            current_cfg["dll"],
            current_cfg.get("steam_loader"),
            "skse64_steam_loader.dll",
            "skse_steam_loader.dll",
        }
        protected_names.discard(None)

        for old_dll in game_dir.glob("skse*.dll"):
            if old_dll.name in protected_names:
                continue

            try:
                if old_dll.exists():
                    try:
                        mode = old_dll.stat().st_mode
                        if not (mode & stat.S_IWRITE):
                            old_dll.chmod(mode | stat.S_IWRITE)
                            logger.debug("Cleared read-only flag on %s", old_dll)
                    except OSError:
                        pass
                    old_dll.unlink()
                    logger.info("Removed orphaned SKSE DLL: %s", old_dll.name)
            except PermissionError as exc:
                logger.warning(
                    "No se pudo eliminar %s (¿Skyrim o MO2 están abiertos?): %s",
                    old_dll.name,
                    exc,
                )
                raise ToolInstallError(
                    f"Permiso denegado al limpiar {old_dll.name}. Cierra Skyrim y Mod Organizer."
                ) from exc
            except OSError as exc:
                logger.warning("Error limpiando %s: %s", old_dll.name, exc)

    async def _download_skse_archive(
        self,
        session: aiohttp.ClientSession,
        cfg: dict[str, str | None],
        dest_path: pathlib.Path,
    ) -> None:
        """Descarga el archivo SKSE vía NetworkGateway con timeout y validación de hash."""

        self._validator.validate(dest_path)

        timeout = aiohttp.ClientTimeout(total=120, sock_read=60)
        downloaded = 0
        hasher = hashlib.sha256()

        url = _skse_cfg_field(cfg, "url")
        expected_sha256 = cfg.get("sha256")

        logger.info("Descargando SKSE desde %s ...", url)

        try:
            resp = await self._gateway.request(
                "GET",
                url,
                session,
                headers={"Accept": "application/octet-stream"},
                timeout=timeout,
                allowed_redirect_hosts=frozenset(["skse.silverlock.org"]),
            )
        except (EgressViolationError, NetworkGatewayTimeoutError) as exc:
            # El contrato del método es ToolInstallError; sin este wrap una denegación
            # de egress escapa cruda y el caller la reporta como fallo desconocido.
            raise ToolInstallError(f"Egress denegado o expirado para SKSE ({url}): {exc}") from exc

        try:
            resp.raise_for_status()

            # Techo de bytes. El `ClientTimeout` de arriba acota cuánto TIEMPO puede
            # durar la descarga, no cuántos bytes entran: contra un origen comprometido
            # o mal configurado que sirva una respuesta infinita, el streaming llena el
            # filesystem de staging sin que ningún timeout lo corte. El Content-Length
            # declarado se chequea primero para ni empezar cuando ya se sabe imposible,
            # pero no se confía en él: puede mentir o faltar, así que el contador real
            # se sigue evaluando chunk a chunk.
            _reject_oversized_skse_content_length(resp)

            with dest_path.open("wb") as fh:
                async for chunk in resp.content.iter_chunked(_DOWNLOAD_CHUNK_SIZE):
                    downloaded += len(chunk)
                    if downloaded > _SKSE_MAX_ARCHIVE_BYTES:
                        raise ToolInstallError(
                            f"La descarga de SKSE excede el tamaño máximo permitido "
                            f"({_SKSE_MAX_ARCHIVE_BYTES} bytes): se aborta antes de llenar el disco."
                        )
                    fh.write(chunk)
                    hasher.update(chunk)

                    if downloaded % (10 * _DOWNLOAD_CHUNK_SIZE) == 0:
                        logger.info("  ... %d bytes descargados", downloaded)
        except ToolInstallError:
            # El techo de bytes ya trae su propio mensaje accionable: propagarlo tal cual
            # y limpiar el parcial (el `finally` de abajo libera la respuesta).
            dest_path.unlink(missing_ok=True)
            raise
        except (aiohttp.ClientError, OSError, TimeoutError) as exc:
            logger.error("Download failed for SKSE: %s", exc)
            if dest_path.exists():
                dest_path.unlink(missing_ok=True)
            raise ToolInstallError(f"Error descargando SKSE: {exc}") from exc
        finally:
            resp.release()

        if downloaded == 0:
            dest_path.unlink(missing_ok=True)
            raise ToolInstallError("SKSE download returned empty content")

        calculated_hash = hasher.hexdigest().upper()
        if expected_sha256 and calculated_hash != expected_sha256.upper():
            dest_path.unlink(missing_ok=True)
            raise ToolInstallError(
                f"Validación de hash fallida para SKSE. Esperado: {expected_sha256}, Calculado: {calculated_hash}"
            )

        logger.info(
            "SKSE descargado (%d bytes, sha256=%s...)",
            downloaded,
            calculated_hash[:16],
        )

    def _find_skse_root(self, extract_path: pathlib.Path, cfg: dict[str, str | None]) -> pathlib.Path:
        """Encuentra el directorio raíz de SKSE, excluyendo __MACOSX y validando estructura."""
        loader_name = _skse_cfg_field(cfg, "loader")
        dll_name = _skse_cfg_field(cfg, "dll")

        valid_loaders = [p for p in extract_path.rglob(loader_name) if "__MACOSX" not in p.parts and p.is_file()]

        if not valid_loaders:
            raise ToolInstallError(
                f"No se encontró {loader_name} válido en el archivo SKSE. "
                "El archivo puede estar corrupto o tener una estructura inesperada."
            )

        if len(valid_loaders) > 1:
            raise ToolInstallError(
                f"Estructura ambigua: se encontraron {len(valid_loaders)} instancias de {loader_name} en el archivo SKSE."
            )

        skse_root = valid_loaders[0].parent

        if not (skse_root / dll_name).exists():
            raise ToolInstallError(f"Payload SKSE incompleto: se encontró el loader pero falta el DLL ({dll_name}).")

        logger.debug("SKSE root encontrado: %s", skse_root)
        return skse_root

    def _assert_safe_copy_target(self, target: pathlib.Path, game_dir: pathlib.Path) -> None:
        """Rechaza *target* como destino de copia si puede escribir fuera de *game_dir*.

        ``shutil.copy2`` sigue los enlaces al escribir, y ``PathValidator`` opina sobre la
        ruta tal cual se la pasa. Con eso solo, un enlace precreado en el directorio del
        juego —o un ancestro enlazado, p. ej. ``Data/`` apuntando afuera— hace que la
        escritura aterrice fuera del sandbox aunque la ruta sin resolver caiga adentro.
        Por eso se rechaza cualquier enlace existente, no solo los que apuntan afuera: un
        enlace a otro archivo DENTRO del juego igual sobrescribe algo que no es el destino.
        """
        if target.is_symlink():
            raise ToolInstallError(
                f"Destino {target} es un enlace simbólico: SKSE no sobrescribe enlaces "
                "(la copia terminaría escribiendo en el archivo apuntado). Borralo y reintentá."
            )

        self._validator.validate(target)

        # El padre real puede no existir todavía (`Data/Scripts/`): se sube hasta el
        # primer ancestro que sí exista, que es el que `mkdir`/`copy2` van a atravesar.
        anchor = target.parent
        while not anchor.exists() and anchor != anchor.parent:
            anchor = anchor.parent

        game_root = game_dir.resolve()
        resolved = anchor.resolve()
        if resolved != game_root and game_root not in resolved.parents:
            raise ToolInstallError(
                f"Destino {target} resuelve fuera del directorio del juego ({resolved}): "
                "hay un enlace simbólico en el camino. Se aborta la instalación."
            )

    async def _copy_skse_files(
        self,
        skse_root: pathlib.Path,
        game_dir: pathlib.Path,
        cfg: dict[str, str | None],
    ) -> None:
        """Copia los binarios de SKSE y la carpeta Data al directorio del juego de forma segura."""

        copied_count = 0

        for item in skse_root.iterdir():
            target = game_dir / item.name

            if item.is_file():
                if item.suffix.lower() in (".dll", ".exe"):
                    self._assert_safe_copy_target(target, game_dir)

                    if target.exists():
                        try:
                            import stat

                            mode = target.stat().st_mode
                            if not (mode & stat.S_IWRITE):
                                target.chmod(mode | stat.S_IWRITE)
                        except OSError:
                            pass
                    # `copy2` es I/O de disco sincrónico: en árboles como el `Data/` de
                    # SKSE bloquea el event loop de NiceGUI (y con él las aprobaciones
                    # HITL) todo lo que dure la copia. Va al threadpool, igual que la
                    # extracción, pero la espera no es cancelable: el hilo copia bajo
                    # el lock y una cancelación no puede liberar el lock con la copia
                    # a medias.
                    await _esperar_hilo_ininterrumpible(shutil.copy2, item, target)
                    copied_count += 1
                    logger.debug("Copiado: %s → %s", item.name, target)
            elif item.is_dir() and item.name == "Data":
                # Validar la copia recursiva archivo por archivo
                for data_item in item.rglob("*"):
                    if data_item.is_file():
                        rel_path = data_item.relative_to(item.parent)
                        data_target = game_dir / rel_path

                        self._assert_safe_copy_target(data_target, game_dir)
                        data_target.parent.mkdir(parents=True, exist_ok=True)
                        await _esperar_hilo_ininterrumpible(shutil.copy2, data_item, data_target)
                        copied_count += 1
                logger.info("Copiada carpeta Data de SKSE")

        if copied_count == 0:
            raise ToolInstallError(
                f"No se copiaron archivos SKSE desde {skse_root}. "
                "Verifica que el archivo descargado contenga los binarios esperados."
            )

        self._verify_skse_installed(skse_root, game_dir, cfg)
        logger.info("SKSE: archivos copiados a %s", game_dir)

    def _verify_skse_installed(
        self,
        skse_root: pathlib.Path,
        game_dir: pathlib.Path,
        cfg: dict[str, str | None],
    ) -> None:
        """Verifica en DESTINO que la instalación de SKSE quedó completa.

        Contar archivos copiados no alcanza: un payload con el loader y un ``Data/``
        poblado deja ``copied_count > 0`` y reportaba éxito aunque el DLL de runtime
        —lo único que hace que SKSE cargue— nunca llegara al directorio del juego.
        """
        loader_name = _skse_cfg_field(cfg, "loader")
        dll_name = _skse_cfg_field(cfg, "dll")

        faltantes = [name for name in (loader_name, dll_name) if not (game_dir / name).is_file()]
        if faltantes:
            raise ToolInstallError(
                f"La instalación de SKSE quedó incompleta en {game_dir}: falta {', '.join(faltantes)}. "
                "El juego no cargaría SKSE. Revisá el archive descargado."
            )

        # El steam loader no entra en el corte duro: solo hace falta para arrancar desde
        # Steam, y MO2 lanza el loader directo. Fallar acá tiraría abajo una instalación
        # que funciona; avisar deja el rastro sin romperla.
        #
        # Pero la expectativa sale del PAYLOAD, no de `SKSE_CONFIG`: los archives no
        # traen todos el mismo set de archivos, así que condicionar el aviso a la
        # edición lo dispara en TODA instalación buena de un build que no lo incluye —
        # y le dice al operador que le falta algo que su build nunca tuvo. Mirando el
        # payload el aviso queda acotado a lo que sí es un defecto: el archive lo traía
        # y no aterrizó (copia incompleta), que es el caso de los builds históricos que
        # realmente lo necesitan. Se compara contra la raíz del payload porque es
        # exactamente el nivel que `_copy_skse_files` recorre: si el archivo no está
        # ahí, no había nada que copiar.
        steam_loader = cfg.get("steam_loader")
        if steam_loader and (skse_root / steam_loader).is_file() and not (game_dir / steam_loader).is_file():
            logger.warning(
                "SKSE instalado sin %s: el arranque desde Steam puede no cargar SKSE "
                "(lanzándolo desde MO2 o desde el loader funciona igual).",
                steam_loader,
            )


async def install_skse(
    installer: Any, game_dir: pathlib.Path, session: aiohttp.ClientSession, *, edition: SkyrimEdition | None = None
) -> InstallResult:
    """Punto de entrada funcional para instalar SKSE con un instalador existente."""
    return await SkseInstaller(installer).ensure_skse(game_dir, session, edition=edition)


__all__ = ["SKSE_CONFIG", "SkseInstaller", "install_skse"]

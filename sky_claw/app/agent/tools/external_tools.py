"""External tools installer for Sky-Claw agent.

Handler for downloading and installing modding tools (LOOT, xEdit, Pandora, BodySlide).
Extracted from tools.py as part of M-13 refactoring.

TASK-011 Tech Debt Cleanup: Removed redundant Pydantic instantiation.
Validation is now centralized in AsyncToolRegistry.execute().
"""

from __future__ import annotations

import asyncio
import json
import logging
import pathlib
from typing import Any

import aiohttp

from sky_claw.app.security.network_gateway import GatewayTCPConnector, NetworkGateway

logger = logging.getLogger(__name__)


def _error_result(message: str) -> dict[str, Any]:
    """Construye el resultado de error canónico."""
    return {"success": False, "message": message}


# Aviso de operación parcial: la tool quedó en disco pero el campo que la hace
# utilizable no se pudo guardar. Corto a propósito — `LLMRouter` trunca el
# resultado a 4000 chars y un JSON de seis tools con `mod_dirs`/`versions` ya es
# voluminoso: un JSON cortado a la mitad le sirve al LLM menos que el defecto que
# este aviso vino a cerrar.
_AVISO_PERSISTENCIA = (
    "Instalado en disco, pero no se pudo guardar la configuración: el registro no "
    "sobrevive al reinicio. Reintentar la instalación es seguro (es idempotente)."
)


async def setup_tools(
    tools_installer: Any,
    install_dir: pathlib.Path,
    local_cfg: Any | None,
    config_path: pathlib.Path | None,
    downloader: Any | None,
    tools: list[str] | None = None,
    *,
    gateway: NetworkGateway | None = None,
    session: aiohttp.ClientSession | None = None,
) -> str:
    """Download and install tools (loot, xedit, pandora, bodyslide).

    Args are pre-validated by AsyncToolRegistry.execute() via SetupToolsParams.

    Consolidation (obs #187): the ``animation_hub`` parameter was removed.
    Installed tool paths are persisted in ``local_cfg``, which the
    AsyncToolRegistry resolvers read at call time — updating an attribute
    on a hub instance was a no-op (the hub read from a frozen config). The
    ``loot_exe_ref`` list parameter was removed for the same reason: callers
    passed a fresh list per invocation, so the post-install mutation never
    propagated; LOOT now follows the same call-time ``local_cfg`` resolution.

    Args:
        tools_installer: ToolsInstaller instance.
        install_dir: Installation directory for tools.
        local_cfg: Config (TOML, lo que inyecta AppContext) o LocalConfig
            (JSON legacy); puede ser None. Los campos se escriben con
            ``escribir_campo`` y se persisten con ``guardar_config`` porque las
            dos clases guardan su estado en lugares distintos: un ``setattr``
            crudo sobre ``Config`` crea un atributo que ningún ``save`` mira.
        config_path: Path to local config file.
        downloader: NexusDownloader instance (may be None).
        tools: List of tools to install. Defaults to all.

    Returns:
        JSON string con una entrada por tool. Una tool que quedó instalada en
        disco pero cuyo campo de config no se pudo guardar se devuelve con
        ``success: False`` y :data:`_AVISO_PERSISTENCIA`, conservando sus campos
        estructurados (``status``, ``exe_path``, ``mods``): la operación fue
        parcial, no exitosa, y reportarla como éxito total es el "éxito visual"
        que la review del PR #445 le hizo corregir al gemelo de la GUI. Un fallo
        al guardar nunca se propaga como excepción — eso perdía el resultado
        entero.
    """
    from sky_claw.local.local_config import escribir_campo, guardar_config_bloqueante
    from sky_claw.local.tools_installer import ToolInstallError

    if tools_installer is None:
        return json.dumps(_error_result("Tools installer is not configured"))

    results: dict[str, Any] = {}
    # Tools cuyo desenlace depende de que la config se persista: si el guardado
    # no ocurre, el path o el registro que acaban de escribir no sobreviven al
    # reinicio y la operación fue parcial, no exitosa. Se registra DESPUÉS de
    # publicar el resultado de éxito (así la invariante "clave registrada ⟺ la
    # entrada es el dict de éxito de esa iteración" se sostiene sola) y con
    # independencia de que `local_cfg` exista: no tener dónde escribir es el
    # mismo desenlace parcial que fallar al escribir.
    requieren_persistencia: list[str] = []

    # TASK-013 P1: Zero-Trust egress policy — a missing NetworkGateway means
    # the integration layer is misconfigured. Abort immediately rather than
    # degrade to an unprotected session that bypasses SSRF/allow-list defences.
    # NOTE: This check is unconditional — even an injected `session` is rejected
    # when gateway=None, preventing a false-success path where the installer
    # runs with an unvetted session that bypasses SSRF/allow-list enforcement.
    if gateway is None:
        logger.error("setup_tools called without NetworkGateway — aborting (Zero-Trust policy)")
        return json.dumps(
            _error_result("NetworkGateway is required for all egress. Configure the gateway before calling this tool.")
        )

    own_session = False
    if session is None:
        session = aiohttp.ClientSession(
            connector=GatewayTCPConnector(gateway, limit=10),
        )
        own_session = True

    try:
        for tool_name in tools or ["loot", "xedit", "pandora", "bodyslide", "ngio"]:
            tool_name_lower = tool_name.lower()
            try:
                if tool_name_lower == "loot":
                    result = await tools_installer.ensure_loot(install_dir, session)
                    if local_cfg:
                        escribir_campo(local_cfg, "loot_exe", str(result.exe_path))
                    results["loot"] = {
                        "status": "already_installed" if result.already_existed else "installed",
                        "exe_path": str(result.exe_path),
                        "version": result.version,
                        "success": True,
                        "message": "",
                    }
                    requieren_persistencia.append("loot")
                elif tool_name_lower == "xedit":
                    result = await tools_installer.ensure_xedit(install_dir, session)
                    if not result.already_existed and local_cfg:
                        escribir_campo(local_cfg, "xedit_exe", str(result.exe_path))
                    results["xedit"] = {
                        "status": "already_installed" if result.already_existed else "installed",
                        "exe_path": str(result.exe_path),
                        "version": result.version,
                        "success": True,
                        "message": "",
                    }
                    # Espeja la condición de arriba: con xEdit preexistente no se
                    # quiso escribir nada, así que un guardado roto no convierte
                    # esta rama en parcial. (Que esa asimetría con los otros
                    # cinco sea un defecto por derecho propio —`xedit_exe` nunca
                    # se rellena en un config que no lo tenga— se reporta aparte;
                    # uniformarla cambia semántica de instalación.)
                    if not result.already_existed:
                        requieren_persistencia.append("xedit")
                elif tool_name_lower == "pandora":
                    result = await tools_installer.ensure_pandora(install_dir, session)
                    if local_cfg:
                        escribir_campo(local_cfg, "pandora_exe", str(result.exe_path))
                    results["pandora"] = {
                        "status": "already_installed" if result.already_existed else "installed",
                        "exe_path": str(result.exe_path),
                        "version": result.version,
                        "success": True,
                        "message": "",
                    }
                    requieren_persistencia.append("pandora")
                elif tool_name_lower == "bodyslide":
                    result = await tools_installer.ensure_bodyslide(install_dir, session, downloader)
                    if local_cfg:
                        escribir_campo(local_cfg, "bodyslide_exe", str(result.exe_path))
                    results["bodyslide"] = {
                        "status": "already_installed" if result.already_existed else "installed",
                        "exe_path": str(result.exe_path),
                        "version": result.version,
                        "success": True,
                        "message": "",
                    }
                    requieren_persistencia.append("bodyslide")
                elif tool_name_lower == "ngio":
                    # Dependencias del precache de grass (Stage 8): se instalan
                    # como mods de MO2 (no tienen exe) y la selección SE/AE la
                    # decide la edición real del ejecutable del juego.
                    from sky_claw.local.discovery.scanner import detect_skyrim_edition

                    mo2_root = getattr(local_cfg, "mo2_root", None) if local_cfg else None
                    skyrim_str = getattr(local_cfg, "skyrim_path", None) if local_cfg else None
                    if not mo2_root:
                        results["ngio"] = _error_result("mo2_root no configurado: corré el escaneo de entorno primero.")
                        continue
                    if not skyrim_str:
                        results["ngio"] = _error_result(
                            "skyrim_path no configurado: no puedo detectar SE vs AE para NGIO."
                        )
                        continue
                    skyrim_exe = pathlib.Path(skyrim_str) / "SkyrimSE.exe"
                    if not skyrim_exe.is_file():
                        detail = (
                            f"No encontré SkyrimSE.exe en {skyrim_str}: NGIO-NG requiere "
                            "Skyrim SE/AE (revisá skyrim_path)."
                        )
                        results["ngio"] = _error_result(detail)
                        continue
                    # pefile hace I/O síncrono de PE: fuera del event loop.
                    edition = await asyncio.to_thread(detect_skyrim_edition, skyrim_exe)
                    mods = await tools_installer.ensure_ngio(
                        pathlib.Path(mo2_root) / "mods",
                        session,
                        downloader,
                        edition=edition,
                    )
                    if local_cfg:
                        escribir_campo(local_cfg, "ngio_mods", [m.mod_name for m in mods])
                    results["ngio"] = {
                        "status": ("already_installed" if all(m.already_existed for m in mods) else "installed"),
                        "edition": edition.value,
                        "mods": [m.mod_name for m in mods],
                        "mod_dirs": [str(m.mod_dir) for m in mods],
                        "versions": {m.mod_name: m.version for m in mods},
                        "success": True,
                        "message": "",
                    }
                    requieren_persistencia.append("ngio")
                elif tool_name_lower == "community_shaders":
                    # Autoinstalador de Community Shaders (blueprint 1786043077717):
                    # mods de MO2 (misma clase que NGIO, NO la de SKSE) cuyos gates
                    # de host — versión del juego, SKSE, ENB, preloader de Engine
                    # Fixes — consultan la raíz del juego, no la intención del
                    # caller. Opt-in deliberado: NO está en la lista default.
                    from sky_claw.local.discovery.scanner import detect_skyrim_edition, read_skyrim_version

                    mo2_root = getattr(local_cfg, "mo2_root", None) if local_cfg else None
                    skyrim_str = getattr(local_cfg, "skyrim_path", None) if local_cfg else None
                    if not mo2_root:
                        results["community_shaders"] = _error_result(
                            "mo2_root no configurado: corré el escaneo de entorno primero."
                        )
                        continue
                    if not skyrim_str:
                        results["community_shaders"] = _error_result(
                            "skyrim_path no configurado: no puedo detectar versión/edición para Community Shaders."
                        )
                        continue
                    skyrim_exe = pathlib.Path(skyrim_str) / "SkyrimSE.exe"
                    if not skyrim_exe.is_file():
                        detail = (
                            f"No encontré SkyrimSE.exe en {skyrim_str}: Community Shaders "
                            "requiere Skyrim SE/AE (revisá skyrim_path)."
                        )
                        results["community_shaders"] = _error_result(detail)
                        continue
                    # pefile hace I/O síncrono de PE: fuera del event loop.
                    edition = await asyncio.to_thread(detect_skyrim_edition, skyrim_exe)
                    game_version = await asyncio.to_thread(read_skyrim_version, skyrim_exe)
                    mods = await tools_installer.ensure_community_shaders(
                        pathlib.Path(mo2_root) / "mods",
                        session,
                        downloader,
                        edition=edition,
                        game_version=game_version,
                        game_dir=pathlib.Path(skyrim_str),
                    )
                    if local_cfg:
                        escribir_campo(local_cfg, "community_shaders_mods", [m.mod_name for m in mods])
                    results["community_shaders"] = {
                        "status": ("already_installed" if all(m.already_existed for m in mods) else "installed"),
                        "edition": edition.value,
                        "game_version": game_version,
                        "mods": [m.mod_name for m in mods],
                        "mod_dirs": [str(m.mod_dir) for m in mods],
                        "versions": {m.mod_name: m.version for m in mods},
                        "success": True,
                        "message": "",
                    }
                    requieren_persistencia.append("community_shaders")
                else:
                    detail = (
                        f"Unknown tool: {tool_name!r}. Supported: loot, xedit, pandora, "
                        "bodyslide, ngio, community_shaders"
                    )
                    results[tool_name] = _error_result(detail)
            except ToolInstallError as exc:
                results[tool_name_lower] = _error_result(str(exc))
            except Exception as exc:
                logger.error("Failed to install %s: %s", tool_name, exc)
                results[tool_name_lower] = _error_result(str(exc))

    finally:
        if own_session and session and not session.closed:
            await session.close()

    # Persist configuration if tools were installed
    # RND-02: Delegar I/O síncrono a thread pool para no bloquear el event loop.
    # `_bloqueante` serializa además con `run_ritual_install` de la GUI (mismo
    # config.toml, mismo proceso) — ver el comentario de módulo en
    # local_config.py sobre por qué hace falta (review PR #444).
    #
    # El guardado NO se condiciona a que alguien se haya registrado: `Config.save()`
    # persiste por generaciones sucias, no por diff de valores (merge-on-save F1),
    # así que saltearlo dejaría colgados los cambios pendientes que otro camino dejó
    # sobre el objeto `Config` compartido.
    persistencia_ok = True
    if local_cfg and config_path:
        try:
            await guardar_config_bloqueante(local_cfg, config_path)
        except Exception:  # noqa: BLE001 — las tools ya están en disco; no perder el resultado
            # Sin esto la excepción se propagaba y se llevaba puesto `results`
            # entero: el caller no se enteraba de que la instalación SÍ había
            # ocurrido. La GUI ya degradaba en vez de romper (`run_ritual_install`).
            logger.exception("No se pudo persistir la configuración en %s", config_path)
            persistencia_ok = False
    elif requieren_persistencia:
        # Sin config o sin su ruta el guardado no puede correr: mismo desenlace
        # parcial que si hubiera lanzado. Cubrir sólo la excepción dejaba vivo el
        # éxito visual — es la corrección que la review adversarial del PR #445 le
        # hizo al gemelo de la GUI (`persistencia_ok = config_path is not None`).
        persistencia_ok = False

    if not persistencia_ok:
        # Sólo se degrada lo que dependía del guardado, y sólo si sigue siendo el
        # dict de éxito: `tools` es una lista sin dedupe y el bucle tampoco
        # deduplica, así que una segunda vuelta pudo haber reemplazado la entrada
        # por un error — pisarle encima este aviso perdería el motivo real.
        for clave in dict.fromkeys(requieren_persistencia):
            entrada = results.get(clave)
            if isinstance(entrada, dict) and entrada.get("success") is True:
                entrada["success"] = False
                entrada["message"] = _AVISO_PERSISTENCIA
        # Si nadie se registró, el fallo queda sólo en el log: ninguna tool de
        # este resultado escribió un campo, así que no hay nada que degradar.

    return json.dumps(results)

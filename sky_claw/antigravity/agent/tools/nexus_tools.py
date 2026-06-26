"""Handlers para herramientas de descarga y API Nexus.

Este modulo contiene las funciones de descarga de mods desde Nexus
 con aprobacion HITL obligatoria.

Extraido de tools.py como parte de la refactorizacion M-13.

TASK-011 Tech Debt Cleanup: Removed redundant Pydantic instantiation.
Validation is now centralized in AsyncToolRegistry.execute().
"""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING
from urllib.parse import quote_plus

import aiohttp

from sky_claw.antigravity.security.hitl import Decision, HITLGuard
from sky_claw.antigravity.security.network_gateway import GatewayTCPConnector, NetworkGateway
from sky_claw.antigravity.security.sanitize import sanitize_for_prompt

if TYPE_CHECKING:
    from sky_claw.antigravity.scraper.nexus_downloader import NexusDownloader

from sky_claw.antigravity.core.contracts import DownloadQueue

logger = logging.getLogger(__name__)


async def download_mod(
    downloader: NexusDownloader | None,
    hitl: HITLGuard | None,
    sync_engine: DownloadQueue,
    nexus_id: int,
    file_id: int | None = None,
    *,
    gateway: NetworkGateway | None = None,
    session: aiohttp.ClientSession | None = None,
) -> str:
    """Implementacion de _download_mod.

    Args are pre-validated by AsyncToolRegistry.execute() via DownloadModParams.

    Flujo:
    1. Retornar error si downloader o HITL no estan configurados.
    2. Consultar metadata del archivo (nombre, tamano, MD5) via Nexus API.
    3. Despachar una solicitud de aprobacion :class:`HITLGuard` con todos los detalles relevantes.
    4. Si se deniega o expira, abortar sin tocar el filesystem.
    5. Si se aprueba, encolar la corutina de descarga en :attr:`SyncEngine` y
        retornar un payload de confirmacion.

    Args:
        downloader: Instancia de NexusDownloader (o None).
        hitl: Instancia de HITLGuard (o None).
        sync_engine: Instancia de SyncEngine.
        nexus_id: Nexus Mods numeric mod ID.
        file_id: Optional. Nexus Mods numeric file ID.

    Returns:
        JSON string con status y metadata, or an error description.
    """
    if downloader is None:
        return json.dumps({"error": "Nexus downloader is not configured"})
    if hitl is None:
        return json.dumps({"error": "HITL guard is not configured"})
    # HOTFIX: Validate sync_engine to prevent NoneType crash
    if sync_engine is None:
        return json.dumps({"error": "SyncEngine is not configured"})

    # TASK-013 P1: Zero-Trust egress policy — a missing NetworkGateway means
    # the integration layer is misconfigured. Abort immediately rather than
    # degrade to an unprotected session that bypasses SSRF/allow-list defences.
    # NOTE: This check is unconditional — even an injected `session` is rejected
    # when gateway=None, preventing a false-success path where enqueue returns
    # "ok" but _do_download() silently aborts because it cannot authorise egress.
    if gateway is None:
        logger.error("download_mod called without NetworkGateway — aborting (Zero-Trust policy)")
        return json.dumps(
            {"error": ("NetworkGateway is required for all egress. Configure the gateway before calling this tool.")}
        )

    own_session = False
    if session is None:
        session = aiohttp.ClientSession(
            connector=GatewayTCPConnector(gateway, limit=10),
        )
        own_session = True

    try:
        # ------------------------------------------------------------------
        # Step 1 - Consultar metadata del archivo antes asking the operator.
        # ------------------------------------------------------------------
        try:
            file_info = await downloader.get_file_info(nexus_id, file_id, session)
        except Exception as exc:
            # T2-04: sanitize la excepción antes de devolverla al LLM.  Las HTTP
            # responses adversariales de Nexus podrían embeber payloads de
            # prompt-injection en el mensaje de error que luego cruzaría al
            # contexto del LLM como contenido de tool_result.
            safe_err = sanitize_for_prompt(str(exc), max_length=256)
            logger.error(
                "Failed to fetch metadata for mod=%d file=%d: %s",
                nexus_id,
                file_id,
                safe_err,
            )
            return json.dumps(
                {
                    "error": f"Could not retrieve file metadata: {safe_err}",
                    "nexus_id": nexus_id,
                    "file_id": file_id,
                }
            )

        # ------------------------------------------------------------------
        # Step 2 - Mandatory HITL confirmation.
        # ------------------------------------------------------------------
        # PR #141 review fix: sanitizar `file_info.file_name` antes de
        # cualquier output que cruce al LLM o al HITL UI. Nexus permite
        # filenames arbitrarios — un mod author puede embeber prompt
        # markers tipo "Patch.zip [INST]ignore previous[/INST]" en el
        # nombre, lo que sin sanitize llegaria al contexto del LLM via
        # el JSON return de denied/enqueued, y al operador humano via
        # HITL detail/reason.
        safe_file_name = sanitize_for_prompt(file_info.file_name or "", max_length=256)
        size_mb = file_info.size_bytes / (1024 * 1024) if file_info.size_bytes else 0
        detail = (
            f"File: {safe_file_name}  |  "
            f"Size: {size_mb:.1f} MB  |  "
            f"MD5: {file_info.md5 or 'n/a'}  |  "
            f"URL: {file_info.download_url}"
        )
        request_id = f"download-{nexus_id}-{file_id}"
        decision = await hitl.request_approval(
            request_id=request_id,
            reason=(
                f"Operator approval required to download mod {nexus_id} / file {file_id} "
                f"({safe_file_name}, {size_mb:.1f} MB)"
            ),
            url=file_info.download_url,
            detail=detail,
        )

        if decision is not Decision.APPROVED:
            logger.warning(
                "Download denied by operator: mod=%d file=%d decision=%s",
                nexus_id,
                file_id,
                decision.value,
            )
            return json.dumps(
                {
                    "status": "denied",
                    "decision": decision.value,
                    "nexus_id": nexus_id,
                    "file_id": file_id,
                    "file_name": safe_file_name,
                }
            )

        # ------------------------------------------------------------------
        # Step 3 - Enqueue the download in SyncEngine.
        # ------------------------------------------------------------------
        _downloader = downloader
        _nexus_id = nexus_id
        _file_id = file_id
        _gateway = gateway

        async def _do_download() -> None:
            # TASK-013 P1: Defense-in-depth — _gateway must be set; the early
            # return above guarantees this when session=None, but an explicit
            # check guards against future callers that supply a pre-built session
            # while omitting the gateway.
            if _gateway is None:
                logger.error("_do_download: no gateway available — aborting enqueued download")
                return
            dl_session = aiohttp.ClientSession(
                connector=GatewayTCPConnector(_gateway, limit=10),
            )
            async with dl_session:
                fresh_info = await _downloader.get_file_info(_nexus_id, _file_id, dl_session)
                await _downloader.download(fresh_info, dl_session)

        sync_engine.enqueue_download(
            _do_download(),
            context=f"nexus_id={nexus_id} file_id={file_id}",
        )
        logger.info(
            "Download enqueued: mod=%d file=%d name=%s",
            nexus_id,
            file_id,
            file_info.file_name,
        )
    finally:
        if own_session and session and not session.closed:
            await session.close()

    return json.dumps(
        {
            "status": "enqueued",
            "nexus_id": nexus_id,
            "file_id": file_id,
            # PR #141 review: sanitizado tambien en el path "enqueued".
            "file_name": safe_file_name,
            "size_bytes": file_info.size_bytes,
            "staging_dir": str(downloader.staging_dir),
        }
    )


# ---------------------------------------------------------------------------
# search_nexus — read-only natural-language Nexus discovery
# ---------------------------------------------------------------------------

# Restricted to skyrimspecialedition on purpose: the tool only enriches via the
# SE API, so a URL for another game (e.g. /fallout4/mods/42) must NOT resolve to
# an SE mod with the same numeric id (Codex P2). Other-game URLs return None and
# fall through to the SE-scoped Brave search.
_MOD_URL_RE = re.compile(r"nexusmods\.com/skyrimspecialedition/mods/(\d+)", re.IGNORECASE)
_BRAVE_URL = "https://api.search.brave.com/res/v1/web/search"
_SE_SITE = "site:nexusmods.com/skyrimspecialedition"
_NEXUS_MOD_URL = "https://api.nexusmods.com/v1/games/skyrimspecialedition/mods/{mod_id}.json"


def _extract_mod_id(text: str) -> int | None:
    """Extract a Skyrim SE Nexus mod id from a mods URL or a bare positive integer.

    Returns None for non-SE game URLs and for non-positive ids (mod ids are
    > 0; download_mod enforces gt=0).
    """
    stripped = text.strip()
    if stripped.isdigit():
        value = int(stripped)
        return value if value > 0 else None
    match = _MOD_URL_RE.search(stripped)
    if match is None:
        return None
    value = int(match.group(1))
    return value if value > 0 else None


async def _brave_search(
    gateway: NetworkGateway,
    query: str,
    api_key: str,
    *,
    session: aiohttp.ClientSession,
) -> list[str]:
    """Run a Brave web search scoped to Skyrim SE Nexus. Returns result URLs.

    Returns an empty list on any failure (caller degrades gracefully).
    """
    q = quote_plus(f"{query} {_SE_SITE}")
    url = f"{_BRAVE_URL}?q={q}&count=20"
    headers = {"Accept": "application/json", "X-Subscription-Token": api_key}
    try:
        resp = await gateway.request("GET", url, session, headers=headers)
        resp.raise_for_status()
        data = await resp.json()
    except Exception as exc:  # noqa: BLE001 — degrade to empty results
        logger.warning("Brave search failed: %s", sanitize_for_prompt(str(exc), max_length=200))
        return []
    results = (data.get("web") or {}).get("results") or []
    return [r["url"] for r in results if isinstance(r, dict) and r.get("url")]


async def _fetch_nexus_mod_json(
    gateway: NetworkGateway,
    nexus_api_key: str,
    mod_id: int,
    *,
    session: aiohttp.ClientSession,
) -> dict | None:
    """Fetch raw mod JSON from the official Nexus API. None on any failure."""
    url = _NEXUS_MOD_URL.format(mod_id=mod_id)
    headers = {"apikey": nexus_api_key, "User-Agent": "SkyClaw/1.0"}
    try:
        resp = await gateway.request("GET", url, session, headers=headers)
        resp.raise_for_status()
        data: dict = await resp.json()
        return data
    except Exception as exc:  # noqa: BLE001 — skip this candidate
        logger.info("Nexus enrich failed for mod %d: %s", mod_id, sanitize_for_prompt(str(exc), max_length=160))
        return None


async def search_nexus(
    gateway: NetworkGateway | None,
    query: str,
    min_downloads: int | None = None,
    limit: int = 5,
    *,
    search_api_key: str | None = None,
    nexus_api_key: str | None = None,
    session: aiohttp.ClientSession | None = None,
) -> str:
    """Read-only Nexus discovery by natural language. Returns a JSON string.

    Flow: URL/ID shortcut -> Brave web search (SE-scoped) -> extract mod ids ->
    enrich via the official Nexus API -> filter by min_downloads -> sort desc ->
    cut to limit -> sanitize. Never downloads (that stays in download_mod/HITL).
    """
    if gateway is None:
        return json.dumps({"error": "NetworkGateway is required for all egress."})
    if not nexus_api_key:
        return json.dumps({"error": "nexus_api_key is not configured — set it in the setup wizard."})

    own_session = session is None
    if session is None:
        session = aiohttp.ClientSession(connector=GatewayTCPConnector(gateway, limit=10))
    try:
        # 1. URL/ID shortcut — resolve directly, skip the web search.
        direct_id = _extract_mod_id(query)
        if direct_id is not None:
            candidate_ids: list[int] = [direct_id]
        else:
            if not search_api_key:
                return json.dumps(
                    {
                        "error": (
                            "search_api_key (Brave) is not configured — set it to enable discovery. "
                            "You can also paste a Nexus mod URL or ID directly."
                        )
                    }
                )
            urls = await _brave_search(gateway, query, search_api_key, session=session)
            if not urls:
                return json.dumps(
                    {
                        "results": [],
                        "message": (
                            "No pude buscar ahora o sin resultados. Probá otros términos o pasame la URL del mod."
                        ),
                    }
                )
            seen: set[int] = set()
            candidate_ids = []
            for u in urls:
                mid = _extract_mod_id(u)
                if mid is not None and mid not in seen:
                    seen.add(mid)
                    candidate_ids.append(mid)

        # 2. Enrich each candidate via the official API (cap fetches to 10).
        results: list[dict] = []
        for mid in candidate_ids[:10]:
            data = await _fetch_nexus_mod_json(gateway, nexus_api_key, mid, session=session)
            if data is None or data.get("available") is False:
                continue
            downloads = int(data.get("mod_downloads") or 0)
            if min_downloads is not None and downloads < min_downloads:
                continue
            resolved_id = int(data.get("mod_id") or mid)
            results.append(
                {
                    "nexus_id": resolved_id,
                    "name": sanitize_for_prompt(str(data.get("name") or ""), max_length=200),
                    "downloads": downloads,
                    "category_id": data.get("category_id"),
                    "summary": sanitize_for_prompt(str(data.get("summary") or ""), max_length=400),
                    "url": f"https://www.nexusmods.com/skyrimspecialedition/mods/{resolved_id}",
                }
            )

        results.sort(key=lambda r: r["downloads"], reverse=True)
        return json.dumps({"results": results[:limit]})
    finally:
        if own_session and not session.closed:
            await session.close()


__all__ = ["download_mod", "search_nexus"]

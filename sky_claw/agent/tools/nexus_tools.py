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
from typing import TYPE_CHECKING

import aiohttp

from sky_claw.security.hitl import Decision, HITLGuard
from sky_claw.security.network_gateway import GatewayTCPConnector, NetworkGateway

if TYPE_CHECKING:
    from sky_claw.scraper.nexus_downloader import NexusDownloader

from sky_claw.core.contracts import DownloadQueue

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
            logger.error(
                "Failed to fetch metadata for mod=%d file=%d: %s",
                nexus_id,
                file_id,
                exc,
            )
            return json.dumps(
                {
                    "error": f"Could not retrieve file metadata: {exc}",
                    "nexus_id": nexus_id,
                    "file_id": file_id,
                }
            )

        # ------------------------------------------------------------------
        # Step 2 - Mandatory HITL confirmation.
        # ------------------------------------------------------------------
        size_mb = file_info.size_bytes / (1024 * 1024) if file_info.size_bytes else 0
        detail = (
            f"File: {file_info.file_name}  |  "
            f"Size: {size_mb:.1f} MB  |  "
            f"MD5: {file_info.md5 or 'n/a'}  |  "
            f"URL: {file_info.download_url}"
        )
        request_id = f"download-{nexus_id}-{file_id}"
        decision = await hitl.request_approval(
            request_id=request_id,
            reason=(
                f"Operator approval required to download "
                f"mod {nexus_id} / file {file_id} "
                f"({file_info.file_name}, {size_mb:.1f} MB)"
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
                    "file_name": file_info.file_name,
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
            "file_name": file_info.file_name,
            "size_bytes": file_info.size_bytes,
            "staging_dir": str(downloader.staging_dir),
        }
    )


__all__ = ["download_mod"]

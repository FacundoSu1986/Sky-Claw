"""Handlers para herramientas de descarga y API Nexus.

Este modulo contiene las funciones de descarga de mods desde Nexus
 con aprobacion HITL obligatoria.

Extraido de tools.py como parte de la refactorizacion M-13.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

import aiohttp

from sky_claw.security.hitl import Decision, HITLGuard
from sky_claw.security.network_gateway import GatewayTCPConnector, NetworkGateway

from .schemas import DownloadModParams

if TYPE_CHECKING:
    from sky_claw.orchestrator.sync_engine import SyncEngine
    from sky_claw.scraper.nexus_downloader import NexusDownloader

logger = logging.getLogger(__name__)


async def download_mod(
    downloader: NexusDownloader | None,
    hitl: HITLGuard | None,
    sync_engine: SyncEngine,
    nexus_id: int,
    file_id: int | None = None,
    *,
    gateway: NetworkGateway | None = None,
    session: aiohttp.ClientSession | None = None,
) -> str:
    """Implementacion de _download_mod.

    Flujo:
    1. Validar parametros con Pydantic.
    2. Retornar error si downloader o HITL no estan configurados.
    3. Consultar metadata del archivo (nombre, tamano, MD5) via Nexus API.
    4. Despachar una solicitud de aprobacion :class:`HITLGuard` con todos los detalles relevantes.
    5. Si se deniega o expira, abortar sin tocar el filesystem.
    6. Si se aprueba, encolar la corutina de descarga en :attr:`SyncEngine` y
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
    params = DownloadModParams(nexus_id=nexus_id, file_id=file_id)

    if downloader is None:
        return json.dumps({"error": "Nexus downloader is not configured"})
    if hitl is None:
        return json.dumps({"error": "HITL guard is not configured"})
    # HOTFIX: Validate sync_engine to prevent NoneType crash
    if sync_engine is None:
        return json.dumps({"error": "SyncEngine is not configured"})

    # S1-FIX: Use injected gateway session; fall back to creating a
    # GatewayTCPConnector-backed session when a gateway is provided.
    own_session = False
    if session is None:
        if gateway is not None:
            session = aiohttp.ClientSession(
                connector=GatewayTCPConnector(gateway, limit=10),
            )
        else:
            logger.warning(
                "download_mod called without gateway — creating unprotected session"
            )
            session = aiohttp.ClientSession()
        own_session = True

    try:
        # ------------------------------------------------------------------
        # Step 1 - Consultar metadata del archivo antes asking the operator.
        # ------------------------------------------------------------------
        try:
            file_info = await downloader.get_file_info(
                params.nexus_id, params.file_id, session
            )
        except Exception as exc:
            logger.error(
                "Failed to fetch metadata for mod=%d file=%d: %s",
                params.nexus_id,
                params.file_id,
                exc,
            )
            return json.dumps(
                {
                    "error": f"Could not retrieve file metadata: {exc}",
                    "nexus_id": params.nexus_id,
                    "file_id": params.file_id,
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
        request_id = f"download-{params.nexus_id}-{params.file_id}"
        decision = await hitl.request_approval(
            request_id=request_id,
            reason=(
                f"Operator approval required to download "
                f"mod {params.nexus_id} / file {params.file_id} "
                f"({file_info.file_name}, {size_mb:.1f} MB)"
            ),
            url=file_info.download_url,
            detail=detail,
        )

        if decision is not Decision.APPROVED:
            logger.warning(
                "Download denied by operator: mod=%d file=%d decision=%s",
                params.nexus_id,
                params.file_id,
                decision.value,
            )
            return json.dumps(
                {
                    "status": "denied",
                    "decision": decision.value,
                    "nexus_id": params.nexus_id,
                    "file_id": params.file_id,
                    "file_name": file_info.file_name,
                }
            )

        # ------------------------------------------------------------------
        # Step 3 - Enqueue the download in SyncEngine.
        # ------------------------------------------------------------------
        _downloader = downloader
        _nexus_id = params.nexus_id
        _file_id = params.file_id
        _gateway = gateway

        async def _do_download() -> None:
            if _gateway is not None:
                dl_session = aiohttp.ClientSession(
                    connector=GatewayTCPConnector(_gateway, limit=10),
                )
            else:
                dl_session = aiohttp.ClientSession()
            async with dl_session:
                fresh_info = await _downloader.get_file_info(
                    _nexus_id, _file_id, dl_session
                )
                await _downloader.download(fresh_info, dl_session)

        sync_engine.enqueue_download(
            _do_download(),
            context=f"nexus_id={params.nexus_id} file_id={params.file_id}",
        )
        logger.info(
            "Download enqueued: mod=%d file=%d name=%s",
            params.nexus_id,
            params.file_id,
            file_info.file_name,
        )
    finally:
        if own_session and session and not session.closed:
            await session.close()

    return json.dumps(
        {
            "status": "enqueued",
            "nexus_id": params.nexus_id,
            "file_id": params.file_id,
            "file_name": file_info.file_name,
            "size_bytes": file_info.size_bytes,
            "staging_dir": str(downloader.staging_dir),
        }
    )


__all__ = ["download_mod"]

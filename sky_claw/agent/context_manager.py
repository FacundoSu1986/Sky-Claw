import asyncio
import logging
import os
from typing import Any

import aiosqlite

# Standard 2026 Context Management
logger = logging.getLogger("SkyClaw.ContextManager")


class ContextManager:
    """
    DYNAMIC CONTEXT MANAGER (STANDARD 2026)

    Implements a sovereign data layer, retrieving local plugin topology and mod
    metadata without external egress. Orchestrates non-blocking I/O for
    LLM prompt injection.
    """

    def __init__(self, db_path: str, mo2_profile_path: str):
        self.db_path: str = db_path
        self.profile_path: str = mo2_profile_path

    async def build_prompt_context(
        self, query: str, target_mods: list[str] | None = None
    ) -> str:
        """
        Synthesizes loadorder status and mod registry metadata into a coherent pre-prompt.
        """
        # Concurrent non-blocking data retrieval
        # H-01: return_exceptions=True para prevenir crashes del orquestador
        mod_results, lo_info = await asyncio.gather(
            self._get_mod_metadata(target_mods or []),
            self._get_load_order(),
            return_exceptions=True,
        )

        context_block: str = "### LOCAL MODDING TOPOLOGY (Zero Trust Edge 2026)\n"
        context_block += f"Load Order Status: {lo_info}\n"
        context_block += "Registry Metadata for Session:\n"

        if mod_results:
            for mod in mod_results:
                status: str = (
                    "✅ Installed/Active" if mod["enabled_in_vfs"] else "⏳ Inactive"
                )
                context_block += f"- [{mod['nexus_id']}] {mod['name']} v{mod['version']} | {status}\n"
        else:
            context_block += (
                "- No matches found in local SQLite registry for specific query mods.\n"
            )

        return context_block

    async def _get_mod_metadata(self, names: list[str]) -> list[dict[str, Any]]:
        """Queries local SQLite registry for mod facts."""
        if not names:
            # Fallback to last updated mods if no specific names provided
            query = "SELECT * FROM mods ORDER BY updated_at DESC LIMIT 5"
            params = ()
        else:
            placeholders = ",".join("?" for _ in names)
            query = "SELECT * FROM mods WHERE name IN (" + placeholders + ")"  # nosec
            params = tuple(names)

        results: list[dict[str, Any]] = []
        try:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                async with db.execute(query, params) as cursor:
                    rows = await cursor.fetchall()
                    results = [dict(row) for row in rows]
        except Exception as e:
            logger.exception(f"Falla en consulta local de metadatos: {e}")

        return results

    async def _get_load_order(self) -> str:
        """Parses loadorder.txt file state via non-blocking access."""
        lo_file: str = os.path.join(self.profile_path, "loadorder.txt")

        if not os.path.exists(lo_file):
            return "Load Order file not found at edge path."

        try:
            # IO-bound operation offloaded to thread executor to keep asyncio loop alive
            data = await asyncio.to_thread(self._read_lo_safe, lo_file)
            return data
        except Exception as e:
            logger.error(f"Fallo de acceso I/O a loadorder.txt: {e}")
            return "Plugin topology unavailable."

    def _read_lo_safe(self, path: str) -> str:
        """Helper for thread-safe file reading."""
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
            plugins = [
                line.strip()
                for line in lines
                if line.strip() and not line.startswith("#")
            ]
            return f"{len(plugins)} active plugins detected."

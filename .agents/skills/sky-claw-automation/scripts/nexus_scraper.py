import asyncio
import json
import sys
import pathlib
import argparse
from typing import Optional

# Ensure the sky_claw package is in the path
project_root = pathlib.Path(__file__).resolve().parents[4] / "sky-claw"
sys.path.append(str(project_root))

from sky_claw.config import Config  # noqa: E402
from sky_claw.security.network_gateway import NetworkGateway  # noqa: E402
from sky_claw.scraper.nexus_downloader import NexusDownloader  # noqa: E402
import aiohttp  # noqa: E402


async def fetch_mod_info(mod_id: int, file_id: Optional[int] = None):
    config = Config()
    gateway = NetworkGateway()

    # We use a dummy mo2 root if not configured, as we only want metadata
    mo2_root = pathlib.Path(config.mo2_root or ".")
    staging_dir = mo2_root / "mods"

    downloader = NexusDownloader(
        api_key=config.nexus_api_key, gateway=gateway, staging_dir=staging_dir
    )

    async with aiohttp.ClientSession() as session:
        try:
            file_info = await downloader.get_file_info(mod_id, file_id, session)
            result = {
                "status": "success",
                "data": {
                    "nexus_id": file_info.nexus_id,
                    "file_id": file_info.file_id,
                    "file_name": file_info.file_name,
                    "size_bytes": file_info.size_bytes,
                    "md5": file_info.md5,
                    "download_url": file_info.download_url,
                },
            }
        except Exception as e:
            result = {"status": "error", "message": str(e)}

    print(json.dumps(result, indent=2))


def main():
    parser = argparse.ArgumentParser(description="Sky-Claw Nexus Scraper Wrapper")
    parser.add_argument("--mod_id", type=int, required=True, help="Nexus Mod ID")
    parser.add_argument("--file_id", type=int, help="Nexus File ID (optional)")

    args = parser.parse_args()

    asyncio.run(fetch_mod_info(args.mod_id, args.file_id))


if __name__ == "__main__":
    main()

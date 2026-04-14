import asyncio
import json
import sys
import pathlib
import argparse
from typing import Any, List, Optional

# Ensure the sky_claw package is in the path
project_root = pathlib.Path(__file__).resolve().parents[4] / "sky-claw"
sys.path.append(str(project_root))

from sky_claw.config import Config  # noqa: E402
from sky_claw.db.async_registry import AsyncModRegistry  # noqa: E402


async def run_db_op(command: str, params: dict):
    config = Config()
    registry = AsyncModRegistry(db_path=config.DB_PATH)
    await registry.open()

    result = {"status": "success", "data": None}
    try:
        if command == "search":
            pattern = params.get("pattern", "")
            mods = await registry.search_mods(pattern)
            result["data"] = mods
        elif command == "get":
            nexus_id = params.get("nexus_id")
            mod = await registry.get_mod(nexus_id)
            if mod:
                result["data"] = dict(mod)
            else:
                result["status"] = "error"
                result["message"] = f"Mod {nexus_id} not found"
        elif command == "list_ids":
            ids = await registry.get_all_nexus_ids()
            result["data"] = list(ids)
        elif command == "upsert":
            nexus_id = params.get("nexus_id")
            name = params.get("name")
            version = params.get("version", "")
            author = params.get("author", "")
            category = params.get("category", "")
            download_url = params.get("download_url", "")
            mod_id = await registry.upsert_mod(
                nexus_id=nexus_id,
                name=name,
                version=version,
                author=author,
                category=category,
                download_url=download_url,
            )
            result["data"] = {"mod_id": mod_id}
        else:
            result["status"] = "error"
            result["message"] = f"Unknown command: {command}"

    except Exception as e:
        result = {"status": "error", "message": str(e)}
    finally:
        await registry.close()

    print(json.dumps(result, indent=2))


def main():
    parser = argparse.ArgumentParser(description="Sky-Claw DB Manager Wrapper")
    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # Search
    search_parser = subparsers.add_parser("search", help="Search mods by name")
    search_parser.add_argument("pattern", help="Search pattern (LIKE %%pattern%%)")

    # Get
    get_parser = subparsers.add_parser("get", help="Get mod by Nexus ID")
    get_parser.add_argument("nexus_id", type=int, help="Nexus Mod ID")

    # List IDs
    subparsers.add_parser("list_ids", help="List all Nexus IDs in the DB")

    # Upsert
    upsert_parser = subparsers.add_parser("upsert", help="Insert or update a mod")
    upsert_parser.add_argument("--nexus_id", type=int, required=True)
    upsert_parser.add_argument("--name", required=True)
    upsert_parser.add_argument("--version", default="")
    upsert_parser.add_argument("--author", default="")
    upsert_parser.add_argument("--category", default="")
    upsert_parser.add_argument("--download_url", default="")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    asyncio.run(run_db_op(args.command, vars(args)))


if __name__ == "__main__":
    main()

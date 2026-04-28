"""Debug utility for testing DeepSeek API connectivity.

NOT for production use — uses structured logging per project invariants.
"""

import asyncio
import logging

import aiohttp

logger = logging.getLogger("SkyClaw.DebugLLM")


async def test_deepseek() -> None:
    """Test DeepSeek API connectivity using keyring-stored credentials."""
    import keyring

    api_key = keyring.get_password("sky_claw", "deepseek_api_key")
    if not api_key:
        logger.warning("No API Key found in keyring for 'deepseek_api_key'.")
        return

    url = "https://api.deepseek.com/v1/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    body = {
        "model": "deepseek-chat",
        "messages": [{"role": "user", "content": "Hola, ¿cómo estás?"}],
        "max_tokens": 1024,
    }

    async with (
        aiohttp.ClientSession() as session,
        session.post(url, headers=headers, json=body) as resp,
    ):
        logger.info("Status: %s", resp.status)
        logger.info("Response: %s", await resp.text())


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(test_deepseek())

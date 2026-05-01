import asyncio
import pathlib
import sys

# Add parent directory to path to import sky_claw
sys.path.append(str(pathlib.Path(__file__).parent.parent))

from sky_claw.config import Config
from sky_claw.local.auto_detect import AutoDetector


async def first_run_wizard():
    print("\n" + "=" * 40)
    print("      Sky-Claw: Asistente de Configuracion")
    print("=" * 40 + "\n")

    config = Config()

    print("[1/3] LLM y API Keys")
    provider = (
        input(f"Proveedor de LLM (anthropic/openai/deepseek/ollama) [{config.llm_provider}]: ").strip().lower()
        or config.llm_provider
    )

    if provider == "openai":
        api_key = input(f"API Key para OpenAI [{config.openai_api_key}]: ").strip() or config.openai_api_key
        model = input(f"Modelo (ej: gpt-4o) [{config.llm_model}]: ").strip() or config.llm_model or "gpt-4o"
    elif provider == "deepseek":
        api_key = input(f"API Key para DeepSeek [{config.deepseek_api_key}]: ").strip() or config.deepseek_api_key
        model = (
            input(f"Modelo (ej: deepseek-chat) [{config.llm_model}]: ").strip() or config.llm_model or "deepseek-chat"
        )
    elif provider == "ollama":
        api_key = ""
        model = input(f"Modelo (ej: llama3.1) [{config.llm_model}]: ").strip() or config.llm_model or "llama3.1"
    else:  # anthropic default
        api_key = input(f"API Key para Anthropic [{config.anthropic_api_key}]: ").strip() or config.anthropic_api_key
        model = (
            input(f"Modelo (ej: claude-3-5-sonnet-20240620) [{config.llm_model}]: ").strip()
            or config.llm_model
            or "claude-3-5-sonnet-20240620"
        )

    nexus_key = input(f"API Key de Nexus Mods (opcional) [{config.nexus_api_key}]: ").strip() or config.nexus_api_key

    print("\n[2/3] Rutas del Sistema")
    print("Escaneando rutas comunes...")
    detected = await AutoDetector.detect_all()

    mo2_root = input(f"Ruta de MO2 Root [{detected.get('mo2_root', config.mo2_root)}]: ").strip() or detected.get(
        "mo2_root", config.mo2_root
    )
    skyrim_path = input(
        f"Ruta de Skyrim [{detected.get('skyrim_path', config.skyrim_path)}]: "
    ).strip() or detected.get("skyrim_path", config.skyrim_path)

    print("\n[3/3] Telegram (Opcional)")
    bot_token = input(f"Telegram Bot Token [{config.telegram_bot_token}]: ").strip() or config.telegram_bot_token
    chat_id = input(f"Telegram Chat ID [{config.telegram_chat_id}]: ").strip() or config.telegram_chat_id

    # Update config data
    config._data["llm_provider"] = provider
    config._data["llm_model"] = model

    if provider == "openai":
        config._data["openai_api_key"] = api_key
    elif provider == "deepseek":
        config._data["deepseek_api_key"] = api_key
    elif provider == "anthropic":
        config._data["anthropic_api_key"] = api_key

    if nexus_key:
        config._data["nexus_api_key"] = nexus_key
    config._data["mo2_root"] = mo2_root
    config._data["skyrim_path"] = skyrim_path
    if bot_token:
        config._data["telegram_bot_token"] = bot_token
    if chat_id:
        config._data["telegram_chat_id"] = chat_id
    config._data["first_run"] = False

    # Save
    config.save()
    print("\n" + "=" * 40)
    print("Configuracion guardada en: " + str(config._config_path))
    print("Ya podes iniciar Sky-Claw!")
    print("=" * 40 + "\n")


if __name__ == "__main__":
    asyncio.run(first_run_wizard())

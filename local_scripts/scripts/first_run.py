import asyncio
import pathlib
import sys

# Add parent directory to path to import sky_claw
sys.path.append(str(pathlib.Path(__file__).parent.parent))

from sky_claw.config import Config
from sky_claw.local.auto_detect import AutoDetector


def _mask_secret(secret: str) -> str:
    """Return a masked preview of *secret* so it is never echoed in clear text.

    Shows only the last 4 characters (e.g. ``****abcd``) so the operator can
    recognise a previously-saved key without exposing it in the terminal
    prompt or scrollback. Empty/short values render as ``****`` or ``""``.
    """
    if not secret:
        return ""
    return f"****{secret[-4:]}" if len(secret) > 4 else "****"


def _validate_path(value: str, *, label: str, require_file: str | None = None) -> tuple[bool, str]:
    """Audit L-1 validation helper for filesystem paths in the wizard.

    Returns ``(True, "")`` when the path is acceptable, ``(False, reason)``
    when it is not.  ``reason`` is a one-line message safe to print on the
    terminal — it names ``label`` and (where relevant) the expected file so
    the user knows what to fix.

    Args:
        value: The path string the user entered.
        label: Human-readable name (e.g. ``"MO2 Root"``) used in error
            messages so the prompt that failed is unambiguous.
        require_file: Optional filename that must exist directly under the
            path (e.g. ``"ModOrganizer.exe"``).  When the directory exists
            but the file is missing, returns ``(False, ...)`` mentioning the
            filename so the operator can recognise the mistake immediately.
    """
    if not value:
        return False, f"La ruta de {label} está vacía."
    candidate = pathlib.Path(value)
    if not candidate.exists():
        return False, f"La ruta de {label} no existe: {value}"
    if not candidate.is_dir():
        return False, f"La ruta de {label} no es un directorio: {value}"
    if require_file is not None and not (candidate / require_file).exists():
        return (
            False,
            f"En {label} no se encontró {require_file} bajo {value}.",
        )
    return True, ""


def _prompt_for_validated_path(
    label: str,
    default: str,
    *,
    require_file: str | None = None,
) -> str:
    """Interactive wrapper around ``_validate_path`` that re-prompts on failure.

    For a *missing-but-otherwise-shaped* path (directory doesn't exist yet,
    or expected ``require_file`` is absent), the user is asked once whether
    to confirm continuing anyway — useful for first-time installs where the
    folder will be created later.

    Empty input is **never** bypassable: an empty value cannot represent a
    valid first-install target, so the helper re-prompts unconditionally.
    This honors the audit L-1 contract that an unset path must not be
    silently persisted (Copilot review on PR #157).
    """
    while True:
        raw = input(f"Ruta de {label} [{default}]: ").strip() or default
        ok, reason = _validate_path(raw, label=label, require_file=require_file)
        if ok:
            return raw
        print(f"  ⚠️  {reason}")
        if not raw:
            # Empty input is non-bypassable: there is nothing to confirm.
            continue
        if input(f"  ¿Continuar igualmente con {raw!r}? [s/N]: ").strip().lower() == "s":
            return raw


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
        api_key = (
            input(f"API Key para OpenAI [{_mask_secret(config.openai_api_key)}]: ").strip() or config.openai_api_key
        )
        model = input(f"Modelo (ej: gpt-4o) [{config.llm_model}]: ").strip() or config.llm_model or "gpt-4o"
    elif provider == "deepseek":
        api_key = (
            input(f"API Key para DeepSeek [{_mask_secret(config.deepseek_api_key)}]: ").strip()
            or config.deepseek_api_key
        )
        model = (
            input(f"Modelo (ej: deepseek-chat) [{config.llm_model}]: ").strip() or config.llm_model or "deepseek-chat"
        )
    elif provider == "ollama":
        api_key = ""
        model = input(f"Modelo (ej: llama3.1) [{config.llm_model}]: ").strip() or config.llm_model or "llama3.1"
    else:  # anthropic default
        api_key = (
            input(f"API Key para Anthropic [{_mask_secret(config.anthropic_api_key)}]: ").strip()
            or config.anthropic_api_key
        )
        model = (
            input(f"Modelo (ej: claude-3-5-sonnet-20240620) [{config.llm_model}]: ").strip()
            or config.llm_model
            or "claude-3-5-sonnet-20240620"
        )

    nexus_key = (
        input(f"API Key de Nexus Mods (opcional) [{_mask_secret(config.nexus_api_key)}]: ").strip()
        or config.nexus_api_key
    )

    print("\n[2/3] Rutas del Sistema")
    print("Escaneando rutas comunes...")
    detected = await AutoDetector.detect_all()

    mo2_default = detected.get("mo2_root", config.mo2_root)
    skyrim_default = detected.get("skyrim_path", config.skyrim_path)
    # Audit L-1: validate user-entered paths before saving so a typo
    # surfaces here rather than during the first real tool run.
    mo2_root = _prompt_for_validated_path("MO2 Root", mo2_default, require_file="ModOrganizer.exe")
    skyrim_path = _prompt_for_validated_path("Skyrim", skyrim_default)

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

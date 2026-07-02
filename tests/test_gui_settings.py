"""Tests de la sección Ajustes del Forge (GUI estática → funcional).

Cubre:
- `validate_credentials` (extraída del SetupWizardModal, seam puro compartido
  entre el wizard first-run y el panel de Ajustes).
- `save_settings`: persistencia de provider/chat_id/identidad en el TOML vía
  Config, secretos a keyring solo si vienen no vacíos ("" = no cambiar), y
  reflejo de la identidad en AppState.
"""

from __future__ import annotations

import pathlib

from sky_claw.antigravity.gui.models.app_state import AppState
from sky_claw.antigravity.gui.setup_wizard import validate_credentials
from sky_claw.antigravity.gui.sky_claw_gui import save_settings


# ── validate_credentials (seam puro compartido wizard/Ajustes) ──────────────────
def test_validate_provider_invalido() -> None:
    assert validate_credentials("gemini", "sk-x") == "Proveedor no válido"


def test_validate_api_key_requerida_en_first_run() -> None:
    # El wizard (first-run) exige API key para proveedores cloud.
    assert validate_credentials("deepseek", "", require_api_key=True) is not None


def test_validate_api_key_opcional_en_ajustes() -> None:
    # En Ajustes, vacío significa "no cambiar la clave existente".
    assert validate_credentials("deepseek", "", require_api_key=False) is None


def test_validate_api_key_demasiado_larga() -> None:
    assert validate_credentials("deepseek", "x" * 513) is not None


def test_validate_telegram_token_sin_dos_puntos() -> None:
    assert validate_credentials("ollama", "", telegram_token="sin-formato", require_api_key=False) is not None


def test_validate_chat_id_no_numerico() -> None:
    assert validate_credentials("ollama", "", telegram_chatid="pepe", require_api_key=False) is not None


def test_validate_todo_valido() -> None:
    assert (
        validate_credentials(
            "anthropic",
            "sk-ant-xxx",
            telegram_token="123:abc",
            telegram_chatid="@123456",
            require_api_key=True,
        )
        is None
    )


# ── save_settings (persistencia de Ajustes) ─────────────────────────────────────
class _FakeKeyring:
    """Backend keyring falso: registra escrituras, devuelve None en lecturas."""

    def __init__(self) -> None:
        self.saved: dict[str, str] = {}

    def set_password(self, service: str, key: str, value: str) -> None:
        self.saved[key] = value

    def get_password(self, service: str, key: str) -> None:
        return None

    def delete_password(self, service: str, key: str) -> None:
        self.saved.pop(key, None)


def _patch_keyring(monkeypatch) -> _FakeKeyring:
    fake = _FakeKeyring()
    import keyring

    monkeypatch.setattr(keyring, "set_password", fake.set_password)
    monkeypatch.setattr(keyring, "get_password", fake.get_password)
    monkeypatch.setattr(keyring, "delete_password", fake.delete_password)
    return fake


def test_save_settings_persiste_provider_identidad_y_chat_id(tmp_path: pathlib.Path, monkeypatch) -> None:
    _patch_keyring(monkeypatch)
    cfg_path = tmp_path / "sky_claw_config.json"
    app_state = AppState(config_path=cfg_path)

    err = save_settings(
        cfg_path,
        {
            "llm_provider": "ollama",
            "telegram_chat_id": "@123456",
            "user_display_name": "Ada Lovelace",
            "user_role": "Forjadora",
        },
        app_state=app_state,
    )
    assert err is None

    # Identidad reflejada en el estado (header data-driven, A3).
    assert app_state.user_display_name == "Ada Lovelace"
    assert app_state.user_role == "Forjadora"

    # Persistido en el TOML: releer con Config lo confirma.
    from sky_claw.config import Config

    cfg = Config(cfg_path)
    assert cfg._data["llm_provider"] == "ollama"
    assert cfg._data["telegram_chat_id"] == "123456"  # sin '@'
    assert cfg._data["user_display_name"] == "Ada Lovelace"
    assert cfg._data["user_role"] == "Forjadora"


def test_save_settings_secretos_vacios_no_tocan_keyring(tmp_path: pathlib.Path, monkeypatch) -> None:
    fake = _patch_keyring(monkeypatch)
    cfg_path = tmp_path / "sky_claw_config.json"

    err = save_settings(
        cfg_path,
        {"llm_provider": "deepseek", "llm_api_key": "", "nexus_api_key": ""},
        app_state=AppState(config_path=cfg_path),
    )
    assert err is None
    assert fake.saved == {}  # "" = no cambiar: nada escrito


def test_save_settings_secretos_no_vacios_van_a_keyring(tmp_path: pathlib.Path, monkeypatch) -> None:
    fake = _patch_keyring(monkeypatch)
    cfg_path = tmp_path / "sky_claw_config.json"

    err = save_settings(
        cfg_path,
        {
            "llm_provider": "deepseek",
            "llm_api_key": "sk-nueva",
            "nexus_api_key": "nx-1",
            "telegram_bot_token": "123:abc",
        },
        app_state=AppState(config_path=cfg_path),
    )
    assert err is None
    assert fake.saved["llm_api_key"] == "sk-nueva"
    assert fake.saved["deepseek_api_key"] == "sk-nueva"  # slot del provider activo
    assert fake.saved["nexus_api_key"] == "nx-1"
    assert fake.saved["telegram_bot_token"] == "123:abc"


def test_save_settings_devuelve_error_de_validacion_sin_persistir(tmp_path: pathlib.Path, monkeypatch) -> None:
    fake = _patch_keyring(monkeypatch)
    cfg_path = tmp_path / "sky_claw_config.json"

    err = save_settings(
        cfg_path,
        {"llm_provider": "gemini"},
        app_state=AppState(config_path=cfg_path),
    )
    assert err == "Proveedor no válido"
    assert fake.saved == {}
    assert not cfg_path.exists()  # no se escribió config

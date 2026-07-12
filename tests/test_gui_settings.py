"""Tests de la sección Ajustes del Forge (GUI estática → funcional).

Cubre:
- `validate_credentials` (extraída del SetupWizardModal, seam puro compartido
  entre el wizard first-run y el panel de Ajustes).
- `save_settings`: persistencia de provider/chat_id/identidad en el TOML vía
  Config, secretos a keyring solo si vienen no vacíos ("" = no cambiar), y
  reflejo de la identidad en AppState.
"""

from __future__ import annotations

import logging
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
    """Backend keyring falso: registra escrituras y las sirve en lecturas.

    ``writes`` acumula solo las escrituras nuevas (para afirmar "no se escribió
    nada" aun cuando ``saved`` venga sembrado con claves preexistentes).
    """

    def __init__(self) -> None:
        self.saved: dict[str, str] = {}
        self.writes: list[tuple[str, str]] = []

    def set_password(self, service: str, key: str, value: str) -> None:
        self.saved[key] = value
        self.writes.append((key, value))

    def get_password(self, service: str, key: str) -> str | None:
        return self.saved.get(key)

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
    fake.saved["deepseek_api_key"] = "sk-existente"  # el provider ya tiene su slot
    cfg_path = tmp_path / "sky_claw_config.json"

    err = save_settings(
        cfg_path,
        {"llm_provider": "deepseek", "llm_api_key": "", "nexus_api_key": ""},
        app_state=AppState(config_path=cfg_path),
    )
    assert err is None
    # "" = no cambiar: la clave existente sigue intacta y no aparecieron nuevas.
    # (Config.save() re-persiste el mismo valor cargado del keyring — round-trip
    # preexistente — así que se afirma sobre el contenido, no sobre las llamadas.)
    assert fake.saved == {"deepseek_api_key": "sk-existente"}


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


# ── Review Codex #221: cambio de provider cloud y limpieza del chat id ──────────
def test_save_settings_exige_clave_al_cambiar_a_provider_sin_slot(tmp_path: pathlib.Path, monkeypatch) -> None:
    """Cambiar a un provider cloud sin clave tipeada NI slot guardado debe fallar:
    AppContext caería a la llm_api_key genérica (la del provider anterior).
    """
    fake = _patch_keyring(monkeypatch)  # keyring sin claves
    cfg_path = tmp_path / "sky_claw_config.json"

    err = save_settings(cfg_path, {"llm_provider": "openai"}, app_state=AppState(config_path=cfg_path))
    assert err is not None
    assert fake.writes == []
    assert not cfg_path.exists()  # el provider nuevo no quedó persistido


def test_save_settings_loguea_si_falla_la_lectura_del_provider_persistido(
    tmp_path: pathlib.Path, monkeypatch, caplog
) -> None:
    """Si Config(config_path) explota al leer el provider persistido (disco,
    TOML corrupto), el except no debe quedar mudo: logger.exception debe
    registrar el fallo. El fallback a current_provider="" se mantiene igual
    que antes (sigue bloqueando, ya que sin provider persistido no hay forma
    de confirmar que la genérica pertenece al provider actual)."""
    import sky_claw.antigravity.gui.sky_claw_gui as gui_module

    _patch_keyring(monkeypatch)  # sin slot propio ni genérica
    cfg_path = tmp_path / "sky_claw_config.json"

    monkeypatch.setattr(
        gui_module,
        "Config",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disco lleno")),
    )

    with caplog.at_level(logging.ERROR, logger=gui_module.logger.name):
        err = save_settings(cfg_path, {"llm_provider": "openai"}, app_state=AppState(config_path=cfg_path))

    assert err is not None  # sin slot y sin poder confirmar el provider persistido: sigue bloqueado
    assert any("provider" in record.getMessage().lower() for record in caplog.records)


def test_save_settings_permite_provider_cloud_con_slot_guardado(tmp_path: pathlib.Path, monkeypatch) -> None:
    fake = _patch_keyring(monkeypatch)
    fake.saved["openai_api_key"] = "sk-previa"  # el usuario ya configuró openai antes
    cfg_path = tmp_path / "sky_claw_config.json"

    err = save_settings(cfg_path, {"llm_provider": "openai"}, app_state=AppState(config_path=cfg_path))
    assert err is None


def test_save_settings_acepta_generica_legacy_sin_cambiar_provider(tmp_path: pathlib.Path, monkeypatch) -> None:
    """Instalación legacy: solo existe la llm_api_key genérica (sin slot por
    provider). Guardar Ajustes SIN cambiar de provider debe aceptarla —
    AppContext la usa como fallback y pertenece al provider actual."""
    from sky_claw.config import Config

    fake = _patch_keyring(monkeypatch)
    fake.saved["llm_api_key"] = "sk-legacy"  # sin openai_api_key
    cfg_path = tmp_path / "sky_claw_config.json"
    cfg = Config(cfg_path)
    cfg._data["llm_provider"] = "openai"  # el provider persistido no cambia
    cfg.save()

    err = save_settings(cfg_path, {"llm_provider": "openai"}, app_state=AppState(config_path=cfg_path))
    assert err is None


def test_save_settings_rechaza_generica_al_cambiar_de_provider(tmp_path: pathlib.Path, monkeypatch) -> None:
    """Candado de la protección Codex #221: la genérica pertenece al provider
    ANTERIOR; cambiar a otro provider cloud sin clave tipeada ni slot propio
    sigue bloqueado (AppContext arrancaría con la clave equivocada)."""
    from sky_claw.config import Config

    fake = _patch_keyring(monkeypatch)
    fake.saved["llm_api_key"] = "sk-del-provider-viejo"
    cfg_path = tmp_path / "sky_claw_config.json"
    cfg = Config(cfg_path)
    cfg._data["llm_provider"] = "deepseek"
    cfg.save()

    err = save_settings(cfg_path, {"llm_provider": "openai"}, app_state=AppState(config_path=cfg_path))
    assert err is not None
    assert Config(cfg_path)._data["llm_provider"] == "deepseek"  # el nuevo no se persistió


def test_save_settings_chat_id_vacio_limpia_el_valor(tmp_path: pathlib.Path, monkeypatch) -> None:
    """El chat id NO es secreto: vaciarlo en Ajustes debe persistir el borrado
    (si no, app_context seguiría notificando al chat viejo para siempre).
    """
    _patch_keyring(monkeypatch)
    cfg_path = tmp_path / "sky_claw_config.json"
    from sky_claw.config import Config

    cfg = Config(cfg_path)
    cfg._data["telegram_chat_id"] = "999888"
    cfg.save()

    err = save_settings(
        cfg_path,
        {"llm_provider": "ollama", "telegram_chat_id": ""},
        app_state=AppState(config_path=cfg_path),
    )
    assert err is None
    assert Config(cfg_path)._data.get("telegram_chat_id", "") == ""


# ── _build_settings_data: badge del proveedor mira también su slot ──────────────
def test_badge_llm_reconoce_el_slot_del_provider(tmp_path: pathlib.Path, monkeypatch) -> None:
    """El resto del sistema considera configurado {provider}_api_key: si solo
    existe ese slot (sin llm_api_key genérica), el badge debe decir Configurada.
    """
    from sky_claw.antigravity.gui.sky_claw_gui import _build_settings_data
    from sky_claw.config import Config

    fake = _patch_keyring(monkeypatch)
    cfg_path = tmp_path / "sky_claw_config.json"
    cfg = Config(cfg_path)
    cfg._data["llm_provider"] = "deepseek"
    cfg.save()

    fake.saved["deepseek_api_key"] = "sk-slot"  # solo el slot del provider

    data = _build_settings_data(cfg_path)
    assert data["provider"] == "deepseek"
    assert data["key_status"]["llm_api_key"] is True

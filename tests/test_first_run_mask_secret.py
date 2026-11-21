"""Regresiones para el enmascarado y la lectura sin eco del asistente inicial.

Validates that ``_mask_secret`` never echoes an API key in clear text:
- a populated key shows only its last 4 characters (``****abcd``)
- short keys collapse to ``****`` (no leak of the full value)
- empty keys render as an empty string
- the original secret value is never returned verbatim
"""

from __future__ import annotations

import getpass
import importlib.util
import io
import pathlib
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

# first_run.py lives under local_scripts/scripts (not an importable package),
# so load it by path the same way the wizard bootstraps itself.
# NOTE: the script mutates sys.path at import time (first_run.py:6). Snapshot
# and restore sys.path around exec_module so that side-effect cannot leak into
# the rest of the test session and make imports order-dependent.
_FIRST_RUN_PATH = pathlib.Path(__file__).parent.parent / "local_scripts" / "scripts" / "first_run.py"
_spec = importlib.util.spec_from_file_location("first_run", _FIRST_RUN_PATH)
assert _spec is not None and _spec.loader is not None
_first_run = importlib.util.module_from_spec(_spec)
_sys_path_snapshot = list(sys.path)
try:
    _spec.loader.exec_module(_first_run)
finally:
    sys.path[:] = _sys_path_snapshot
_mask_secret = _first_run._mask_secret


@pytest.mark.parametrize(
    ("secret", "expected"),
    [
        ("sk-1234567890abcd", "****abcd"),  # long key → last 4 only
        ("ab", "****"),  # short key → fully masked
        ("abcd", "****"),  # exactly 4 → fully masked (no leak)
        ("", ""),  # empty → empty preview
    ],
)
def test_mask_secret_previews(secret: str, expected: str) -> None:
    assert _mask_secret(secret) == expected


def test_mask_secret_never_returns_full_secret() -> None:
    """A realistic key must not appear verbatim in the masked preview."""
    secret = "sk-proj-SUPERSECRETvalue1234"
    masked = _mask_secret(secret)
    assert secret not in masked
    assert masked == "****1234"
    # Only the trailing 4 chars are exposed; the sensitive prefix is gone.
    assert "SUPERSECRET" not in masked


@pytest.mark.parametrize("provider", ["openai", "deepseek", "anthropic", "ollama"])
@pytest.mark.parametrize("replace", [False, True], ids=["conservar", "reemplazar"])
async def test_el_asistente_oculta_los_secretos_y_guarda_los_valores_elegidos(
    provider: str, replace: bool, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    secretos = {
        "openai_api_key": "canario-openai-viejo",
        "deepseek_api_key": "canario-deepseek-viejo",
        "anthropic_api_key": "canario-anthropic-viejo",
        "nexus_api_key": "canario-nexus-viejo",
        "telegram_bot_token": "canario-telegram-viejo",
    }
    (tmp_path / "ModOrganizer.exe").touch()
    config = SimpleNamespace(
        **secretos,
        llm_provider=provider,
        openai_model="",
        deepseek_model="",
        anthropic_model="",
        ollama_model="",
        mo2_root=str(tmp_path),
        skyrim_path=str(tmp_path),
        telegram_chat_id="",
        _data={},
        _config_path=tmp_path / "config.toml",
        save=Mock(),
    )
    monkeypatch.setattr(_first_run, "Config", lambda: config)
    monkeypatch.setattr(_first_run.AutoDetector, "detect_all", AsyncMock(return_value={}))
    visibles = iter(["", "modelo-elegido", "", "", "12345"])
    preguntas_visibles = []

    def leer_visible(prompt: str) -> str:
        print(prompt, end="")
        preguntas_visibles.append(prompt)
        if prompt.startswith(("API Key", "Telegram Bot Token")):
            return ""
        return next(visibles)

    claves_pedidas = ["nexus_api_key", "telegram_bot_token"]
    if provider != "ollama":
        claves_pedidas.insert(0, f"{provider}_api_key")
    nuevos = {clave: f"canario-nuevo-{clave}" for clave in claves_pedidas}
    respuestas = iter(f" {nuevos[clave]} " if replace else "" for clave in claves_pedidas)
    preguntas_ocultas = []

    def leer_oculto(prompt: str) -> str:
        print(prompt, end="", file=sys.stderr)
        preguntas_ocultas.append(prompt)
        return next(respuestas)

    monkeypatch.setattr("builtins.input", leer_visible)
    monkeypatch.setattr(getpass, "getpass", leer_oculto)

    await _first_run.first_run_wizard()

    consola = capsys.readouterr()
    assert all(secreto not in consola.out + consola.err for secreto in (*secretos.values(), *nuevos.values()))
    assert len(preguntas_visibles) == 5
    assert len(preguntas_ocultas) == len(claves_pedidas)
    for clave, pregunta in zip(claves_pedidas, preguntas_ocultas, strict=True):
        assert _mask_secret(secretos[clave]) in pregunta
        assert config._data[clave] == (nuevos[clave] if replace else secretos[clave])
    assert config._data[f"{provider}_model"] == "modelo-elegido"
    assert config._data["telegram_chat_id"] == "12345"
    assert config._data["first_run"] is False
    config.save.assert_called_once_with()


def test_el_prompt_se_detiene_si_getpass_no_puede_evitar_el_eco(monkeypatch: pytest.MonkeyPatch) -> None:
    leer_con_eco = Mock(side_effect=AssertionError("Se intentó leer con eco"))
    monkeypatch.setattr(getpass, "_raw_input", leer_con_eco)
    monkeypatch.setattr(getpass, "getpass", lambda prompt: getpass.fallback_getpass(prompt, stream=io.StringIO()))

    with pytest.raises(getpass.GetPassWarning):
        _first_run._prompt_for_secret("Telegram Bot Token", "canario-guardado")

    leer_con_eco.assert_not_called()

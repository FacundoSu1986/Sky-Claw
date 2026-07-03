"""Tests del hot-reload del proveedor LLM al guardar Ajustes.

El swap del proveedor vivía duplicado (``frontend_bridge._do_llm_reload`` con
keyring + acceso directo a ``router._provider``, y ``router.reload_provider``
con vault) y el Forge no lo disparaba: guardar Ajustes solo decía "aplica al
reiniciar". Este módulo centraliza el swap en:

- ``LLMRouter.set_provider`` — seam público, intercambio atómico bajo lock.
- ``AppContext.reload_llm_provider`` — resuelve clave (keyring, con fallback a
  la genérica) y modelo por-provider, arma el provider y llama a ``set_provider``.

El feedback del toast del GUI se decide en un seam puro (``_llm_reload_feedback``).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest


# ── LLMRouter.set_provider: seam público de swap atómico ─────────────────────────
class _FakeProvider:
    def __init__(self, name: str) -> None:
        self.name = name


def _make_router() -> Any:
    """Construye un LLMRouter mínimo sin abrir DB ni red (solo el lock + provider)."""
    from sky_claw.antigravity.agent.router import LLMRouter

    router = LLMRouter.__new__(LLMRouter)
    router._provider = _FakeProvider("viejo")
    router._provider_lock = asyncio.Lock()
    return router


async def test_set_provider_intercambia_bajo_lock() -> None:
    router = _make_router()
    nuevo = _FakeProvider("nuevo")
    await router.set_provider(nuevo)
    assert router._provider is nuevo


# ── AppContext.reload_llm_provider ──────────────────────────────────────────────
def _make_ctx(monkeypatch: pytest.MonkeyPatch, *, router: Any, keys: dict[str, str], model: str = "") -> Any:
    """AppContext desnudo con router y config_path stubbeados + keyring/create_provider fake."""
    import sky_claw.app_context as ac

    ctx = ac.AppContext.__new__(ac.AppContext)
    ctx.router = router
    ctx.config_path = None  # forzamos la lectura de modelo por monkeypatch de Config

    monkeypatch.setattr(ac.keyring, "get_password", lambda service, key: keys.get(key))

    created: dict[str, Any] = {}

    def _fake_create(*, provider_name: str, api_key: str = "", model: str = "") -> _FakeProvider:
        created["provider_name"] = provider_name
        created["api_key"] = api_key
        created["model"] = model
        return _FakeProvider(provider_name)

    monkeypatch.setattr(ac, "create_provider", _fake_create)
    return ctx, created


async def test_reload_usa_clave_del_provider_y_hace_swap(monkeypatch: pytest.MonkeyPatch) -> None:
    router = _make_router()
    ctx, created = _make_ctx(monkeypatch, router=router, keys={"anthropic_api_key": "sk-ant"})
    ok = await ctx.reload_llm_provider("anthropic")
    assert ok is True
    assert created["provider_name"] == "anthropic"
    assert created["api_key"] == "sk-ant"
    assert router._provider.name == "anthropic"


async def test_reload_api_key_explicita_tiene_prioridad(monkeypatch: pytest.MonkeyPatch) -> None:
    router = _make_router()
    ctx, created = _make_ctx(monkeypatch, router=router, keys={"anthropic_api_key": "vieja"})
    await ctx.reload_llm_provider("anthropic", api_key="tipeada-ahora")
    assert created["api_key"] == "tipeada-ahora"


async def test_reload_cae_a_llm_api_key_generica(monkeypatch: pytest.MonkeyPatch) -> None:
    router = _make_router()
    # Sin clave específica del provider; usa la genérica (comportamiento de _do_llm_reload).
    ctx, created = _make_ctx(monkeypatch, router=router, keys={"llm_api_key": "generica"})
    ok = await ctx.reload_llm_provider("deepseek")
    assert ok is True
    assert created["api_key"] == "generica"


async def test_reload_ollama_no_requiere_clave(monkeypatch: pytest.MonkeyPatch) -> None:
    router = _make_router()
    ctx, created = _make_ctx(monkeypatch, router=router, keys={})
    ok = await ctx.reload_llm_provider("ollama")
    assert ok is True
    assert created["provider_name"] == "ollama"


async def test_reload_sin_clave_para_cloud_devuelve_false(monkeypatch: pytest.MonkeyPatch) -> None:
    router = _make_router()
    viejo = router._provider
    ctx, _ = _make_ctx(monkeypatch, router=router, keys={})
    ok = await ctx.reload_llm_provider("anthropic")
    assert ok is False
    assert router._provider is viejo  # no se tocó


async def test_reload_sin_router_devuelve_false(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx, _ = _make_ctx(monkeypatch, router=None, keys={"anthropic_api_key": "sk"})
    assert await ctx.reload_llm_provider("anthropic") is False


async def test_reload_provider_defectuoso_devuelve_false(monkeypatch: pytest.MonkeyPatch) -> None:
    import sky_claw.app_context as ac

    router = _make_router()
    viejo = router._provider
    ctx, _ = _make_ctx(monkeypatch, router=router, keys={"anthropic_api_key": "sk"})

    def _boom(**_: Any) -> Any:
        raise ac.ProviderConfigError("clave inválida")

    monkeypatch.setattr(ac, "create_provider", _boom)
    ok = await ctx.reload_llm_provider("anthropic")
    assert ok is False
    assert router._provider is viejo


async def test_reload_respeta_el_modelo_por_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bug latente de _do_llm_reload: el swap perdía el modelo configurado."""
    import sky_claw.app_context as ac

    router = _make_router()
    ctx, created = _make_ctx(monkeypatch, router=router, keys={"anthropic_api_key": "sk"})
    ctx.config_path = "/fake/config.toml"
    monkeypatch.setattr(ac, "Config", lambda _p: SimpleNamespace(anthropic_model="claude-opus-4-8"))
    await ctx.reload_llm_provider("anthropic")
    assert created["model"] == "claude-opus-4-8"


# ── Seam puro del feedback del toast (GUI) ──────────────────────────────────────
def test_feedback_ok_es_positivo() -> None:
    from sky_claw.antigravity.gui.sky_claw_gui import _llm_reload_feedback

    fb = _llm_reload_feedback(True, "anthropic")
    assert fb["type"] == "positive"
    assert "Anthropic" in fb["text"]


def test_feedback_fallo_es_warning_y_menciona_reinicio() -> None:
    from sky_claw.antigravity.gui.sky_claw_gui import _llm_reload_feedback

    fb = _llm_reload_feedback(False, "deepseek")
    assert fb["type"] == "warning"
    assert "reiniciar" in fb["text"].lower()

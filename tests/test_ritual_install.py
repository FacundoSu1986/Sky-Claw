"""Tests del Follow-up C — botón "Instalar" de los Rituales en estado "No instalado".

El estado ``missing`` de cada tarjeta de Ritual ahora cablea al ``ToolsInstaller``
existente (``ensure_loot``/``ensure_xedit``/``ensure_pandora``), reusando el patrón
single-flight + feedback de ``run_ritual``. La aprobación de descarga se enruta por el
puente HITL de la GUI con categoría propia ``download`` (parkeada en el modal y **nunca**
auto-aprobada por «Modo local»). Estos tests cubren las costuras puras (mapeo, flujo de
instalación, decisión del puente) sin un cliente NiceGUI vivo.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import pytest

from sky_claw.antigravity.gui.controllers.ritual_runner import (
    RITUAL_INSTALL_ENV,
    RITUAL_INSTALLER_MAP,
    make_gui_hitl_notify,
    ritual_installer_name,
    run_ritual_install,
)
from sky_claw.antigravity.gui.state.reactive_store import ReactiveStore
from sky_claw.local.tools_installer import ToolInstallError


@dataclass
class _FakeReq:
    request_id: str
    category: str = "tool_execution"
    reason: str = "approval"
    detail: str = "payload: <empty>"
    url: str = ""


@dataclass
class _FakeInstallResult:
    exe_path: str
    already_existed: bool = False


class _FakeInstaller:
    """Installer doble: registra qué ensure_* se llamó y devuelve/lanza lo configurado."""

    def __init__(self, *, exe_path: str = "C:/Modding/LOOT/loot.exe", error: Exception | None = None) -> None:
        self.calls: list[tuple[str, object, object]] = []
        self._exe_path = exe_path
        self._error = error

    async def _ensure(self, name: str, install_dir: object, session: object) -> _FakeInstallResult:
        self.calls.append((name, install_dir, session))
        if self._error is not None:
            raise self._error
        return _FakeInstallResult(exe_path=self._exe_path)

    async def ensure_loot(self, install_dir: object, session: object) -> _FakeInstallResult:
        return await self._ensure("ensure_loot", install_dir, session)

    async def ensure_xedit(self, install_dir: object, session: object) -> _FakeInstallResult:
        return await self._ensure("ensure_xedit", install_dir, session)

    async def ensure_pandora(self, install_dir: object, session: object) -> _FakeInstallResult:
        return await self._ensure("ensure_pandora", install_dir, session)


class _FakeAppContext:
    def __init__(self, installer: object, *, install_dir: object = "C:/Modding", session: object = "sess") -> None:
        self.tools_installer = installer
        self.install_dir = install_dir
        self.session = session


# ── Ritual → installer mapping ──────────────────────────────────────────────────
def test_ritual_installer_map_covers_only_github_backed_tools() -> None:
    assert RITUAL_INSTALLER_MAP == {
        "loot": "ensure_loot",
        "xedit": "ensure_xedit",
        "pandora": "ensure_pandora",
    }


def test_ritual_installer_name_known_and_unmapped() -> None:
    assert ritual_installer_name("loot") == "ensure_loot"
    assert ritual_installer_name("xedit") == "ensure_xedit"
    assert ritual_installer_name("pandora") == "ensure_pandora"
    # Wrye Bash / DynDOLOD no tienen instalador automático (no están en GitHub releases).
    assert ritual_installer_name("wrye_bash") is None
    assert ritual_installer_name("dyndolod") is None


def test_install_env_map_matches_resolver_vars() -> None:
    assert RITUAL_INSTALL_ENV == {
        "loot": "LOOT_EXE",
        "xedit": "XEDIT_PATH",
        "pandora": "PANDORA_EXE",
    }


# ── run_ritual_install flujo ────────────────────────────────────────────────────
async def test_install_success_seeds_env_and_publishes_positive(monkeypatch: pytest.MonkeyPatch) -> None:
    # setenv (not delenv) so monkeypatch's teardown pops LOOT_EXE even though the
    # code under test sets it directly — otherwise the value leaks into later tests.
    monkeypatch.setenv("LOOT_EXE", "")
    store = ReactiveStore()
    installer = _FakeInstaller(exe_path="C:/Modding/LOOT/loot.exe")
    ctx = _FakeAppContext(installer)

    await run_ritual_install("loot", app_context=ctx, store=store)

    assert installer.calls == [("ensure_loot", "C:/Modding", "sess")]
    assert os.environ.get("LOOT_EXE") == "C:/Modding/LOOT/loot.exe"
    fb = store.get("ritual_feedback")
    assert fb is not None and fb["type"] == "positive"
    assert not store.get("ritual_in_flight")


async def test_install_unmapped_tool_does_not_install(monkeypatch: pytest.MonkeyPatch) -> None:
    store = ReactiveStore()
    installer = _FakeInstaller()
    ctx = _FakeAppContext(installer)

    await run_ritual_install("wrye_bash", app_context=ctx, store=store)

    assert installer.calls == []
    fb = store.get("ritual_feedback")
    assert fb is not None and fb["type"] == "info"


async def test_install_without_installer_reports_error() -> None:
    store = ReactiveStore()
    ctx = _FakeAppContext(installer=None)

    await run_ritual_install("loot", app_context=ctx, store=store)

    fb = store.get("ritual_feedback")
    assert fb is not None and fb["type"] == "negative"


async def test_install_denied_or_failed_is_negative() -> None:
    store = ReactiveStore()
    installer = _FakeInstaller(error=ToolInstallError("denied by operator"))
    ctx = _FakeAppContext(installer)

    await run_ritual_install("xedit", app_context=ctx, store=store)

    assert installer.calls == [("ensure_xedit", "C:/Modding", "sess")]
    fb = store.get("ritual_feedback")
    assert fb is not None and fb["type"] == "negative"
    assert not store.get("ritual_in_flight")


async def test_install_unexpected_exception_is_negative_and_never_raises() -> None:
    store = ReactiveStore()
    installer = _FakeInstaller(error=RuntimeError("boom"))
    ctx = _FakeAppContext(installer)

    # Debe convertir la excepción en feedback, no propagar (fire-and-forget task).
    await run_ritual_install("pandora", app_context=ctx, store=store)

    fb = store.get("ritual_feedback")
    assert fb is not None and fb["type"] == "negative"


async def test_install_single_flight_refuses_while_one_is_in_flight() -> None:
    store = ReactiveStore()
    store.set("ritual_in_flight", True)  # un ritual/instalación ya en curso
    installer = _FakeInstaller()
    ctx = _FakeAppContext(installer)

    await run_ritual_install("loot", app_context=ctx, store=store)

    assert installer.calls == []  # no debe instalar
    fb = store.get("ritual_feedback")
    assert fb is not None and fb["type"] == "warning"


async def test_install_clears_pending_prompt_on_finish() -> None:
    store = ReactiveStore()
    store.set("pending_hitl", {"request_id": "install-loot-1"})  # prompt parkeado de esta corrida
    installer = _FakeInstaller()
    ctx = _FakeAppContext(installer)

    await run_ritual_install("loot", app_context=ctx, store=store)

    assert store.get("pending_hitl") is None
    assert not store.get("ritual_in_flight")


# ── Puente HITL de la GUI: categoría download ───────────────────────────────────
async def test_bridge_parks_download_modal_and_never_auto_approves() -> None:
    responded: list[tuple[str, bool]] = []
    pending: list[dict] = []

    async def _respond(rid: str, approved: bool) -> None:  # pragma: no cover - no debe correr
        responded.append((rid, approved))

    # Modo local ON: una descarga de red SIEMPRE se confirma a mano (egress).
    notify = make_gui_hitl_notify(
        respond=_respond,
        set_pending=pending.append,
        auto_approve_getter=lambda: True,
        delegate=None,
    )
    await notify(
        _FakeReq("install-loot-1", category="download", reason="Install LOOT?", detail="Asset…", url="https://x/y.zip")
    )

    assert responded == []  # nunca auto-aprobado
    assert pending == [
        {
            "request_id": "install-loot-1",
            "reason": "Install LOOT?",
            "detail": "Asset…",
            "url": "https://x/y.zip",
        }
    ]

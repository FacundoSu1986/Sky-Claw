"""Tests del Follow-up C — botón "Instalar" de los Rituales en estado "No instalado".

El estado ``missing`` de cada tarjeta de Ritual ahora cablea al ``ToolsInstaller``
existente (``ensure_loot``/``ensure_xedit``/``ensure_pandora``), reusando el patrón
single-flight + feedback de ``run_ritual``. La aprobación de descarga se enruta por el
puente HITL de la GUI con categoría propia ``download`` (parkeada en el modal y **nunca**
auto-aprobada por «Modo local»). Estos tests cubren las costuras puras (mapeo, flujo de
instalación, decisión del puente) sin un cliente NiceGUI vivo.
"""

from __future__ import annotations

import inspect
import os
import pathlib
import re
from dataclasses import dataclass

import pytest

from sky_claw.app.agent.tools import external_tools
from sky_claw.app.gui.controllers.ritual_runner import (
    RITUAL_INSTALL_ENV,
    RITUAL_INSTALLER_MAP,
    STORE_KEY_RITUAL_PREFLIGHT,
    make_gui_hitl_notify,
    ritual_installer_name,
    run_ritual_install,
)
from sky_claw.app.gui.state.reactive_store import ReactiveStore
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

    def __init__(
        self,
        *,
        exe_path: str = "C:/Modding/LOOT/loot.exe",
        error: Exception | None = None,
        cs_result: list | None = None,
    ) -> None:
        self.calls: list[tuple[str, object, object]] = []
        self._exe_path = exe_path
        self._error = error
        self._cs_result = cs_result or []
        self.cs_kwargs: dict[str, object] | None = None
        self.cs_downloader: object = None

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

    async def ensure_skse(self, install_dir: object, session: object) -> _FakeInstallResult:
        return await self._ensure("ensure_skse", install_dir, session)

    async def ensure_community_shaders(
        self, mods_dir: object, session: object, downloader: object = None, **kwargs: object
    ) -> list:
        self.calls.append(("ensure_community_shaders", mods_dir, session))
        self.cs_downloader = downloader
        self.cs_kwargs = kwargs
        if self._error is not None:
            raise self._error
        return self._cs_result


class _FakeAppContext:
    def __init__(
        self,
        installer: object,
        *,
        install_dir: object = "C:/Modding",
        session: object = "sess",
        network: object = None,
        config_path: object = None,
    ) -> None:
        self.tools_installer = installer
        self.install_dir = install_dir
        self.session = session
        self.network = network
        self.config_path = config_path


# ── Ritual → installer mapping ──────────────────────────────────────────────────
def test_ritual_installer_map_congela_las_tools_autoinstalables() -> None:
    # Wrye Bash y DynDOLOD siguen afuera (no están en GitHub releases). SKSE entra
    # por su propio origen oficial (skse.silverlock.org), habilitado en ALLOWED_HOSTS.
    # Community Shaders entra con su rama propia (firma ≠ ensure(install_dir, session)):
    # ver `run_ritual_install` — si la rama desaparece sin tocar este mapa, el botón
    # "Instalar" de la tarjeta degrada a "no tiene instalación automática" (ancla).
    assert RITUAL_INSTALLER_MAP == {
        "loot": "ensure_loot",
        "xedit": "ensure_xedit",
        "pandora": "ensure_pandora",
        "skse": "ensure_skse",
        "community_shaders": "ensure_community_shaders",
    }


def test_skse_es_gui_only_y_no_lo_alcanza_el_agente_llm() -> None:
    """Instalar SKSE escribe ejecutables en el directorio del juego tras un egress.

    Esa aprobación tiene que ser la del operador frente a la GUI, no una decisión del
    modelo. Se afirma por igualdad literal sobre el set de tools que ``setup_tools``
    despacha: agregar una rama nueva ahí rompe el ancla, que es exactamente el aviso
    que hace falta cuando la rama nueva es una mutación.

    ``community_shaders`` SÍ entra en el set a propósito: es la clase NGIO (mods de
    MO2 con gates de host), no la clase SKSE (ejecutables en el juego). No es un
    olvido — el recorte de SKSE sigue siendo exclusivo de SKSE.
    """
    fuente = inspect.getsource(external_tools.setup_tools)
    despachadas = set(re.findall(r'tool_name_lower == "(\w+)"', fuente))

    assert despachadas == {"loot", "xedit", "pandora", "bodyslide", "ngio", "community_shaders"}
    assert "skse" not in despachadas
    # …y sí está del lado de la GUI, para que el recorte sea una decisión visible y no
    # un olvido que deja la feature inalcanzable desde ambas superficies.
    assert RITUAL_INSTALLER_MAP["skse"] == "ensure_skse"


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


async def test_install_skse_usa_la_ruta_del_juego_no_el_install_dir_generico() -> None:
    """SKSE se instala DENTRO del directorio del juego, no en `install_dir`.

    `install_dir` (`app_context.install_dir`) es el directorio genérico donde van
    LOOT/xEdit/Pandora — MO2 y los tools viven aparte de Skyrim. Sin este ruteo,
    `ensure_skse` recibe esa carpeta genérica, busca `SkyrimSE.exe` ahí adentro,
    no lo encuentra, y el botón "Instalar" de SKSE falla siempre.
    """
    from sky_claw.app.gui.views.forge_dashboard import STORE_KEY_ENV
    from sky_claw.local.discovery.environment import EnvironmentSnapshot, SkyrimEdition, SkyrimInfo

    game_path = pathlib.Path("D:/Steam/steamapps/common/Skyrim Special Edition")
    store = ReactiveStore()
    store.set(
        STORE_KEY_ENV,
        EnvironmentSnapshot(skyrim=SkyrimInfo(path=game_path, exe_name="SkyrimSE.exe", edition=SkyrimEdition.AE)),
    )
    installer = _FakeInstaller(exe_path=str(game_path / "skse64_loader.exe"))
    ctx = _FakeAppContext(installer, install_dir="C:/Modding")

    await run_ritual_install("skse", app_context=ctx, store=store)

    assert installer.calls == [("ensure_skse", game_path, "sess")]
    fb = store.get("ritual_feedback")
    assert fb is not None and fb["type"] == "positive"


async def test_install_skse_sin_snapshot_de_skyrim_no_instala_en_install_dir() -> None:
    """Sin la carpeta del juego detectada, cortar es preferible a instalar en el
    directorio genérico (que llevaría al mismo fallo de siempre buscando el .exe
    donde no está, ya cubierto por `test_sin_ejecutable_del_juego_no_adivina_la_edicion`
    en tools_installer — acá se cubre que la GUI ni siquiera llega a intentarlo).
    """
    store = ReactiveStore()
    installer = _FakeInstaller()
    ctx = _FakeAppContext(installer, install_dir="C:/Modding")

    await run_ritual_install("skse", app_context=ctx, store=store)

    assert installer.calls == []
    fb = store.get("ritual_feedback")
    assert fb is not None and fb["type"] == "negative"


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
            "owner_tab": None,
        }
    ]


# ── run_ritual_install: rama community_shaders ──────────────────────────────────
async def test_install_community_shaders_usa_mo2_y_juego_del_snapshot(tmp_path: pathlib.Path) -> None:
    """La rama GUI de CS resuelve mo2_root/mods + juego del snapshot y despacha con kwargs.

    `ensure_community_shaders` NO tiene la firma ensure(install_dir, session): recibe
    mods/ (bajo mo2_root), el NexusDownloader de app_context y edition/game_version/
    game_dir del snapshot — la misma fuente que decidió mostrar la tarjeta.
    """
    from sky_claw.app.gui.views.forge_dashboard import STORE_KEY_ENV
    from sky_claw.local.discovery.environment import EnvironmentSnapshot, MO2Info, SkyrimEdition, SkyrimInfo
    from sky_claw.local.tools_installer import ModInstallResult

    mo2_root = pathlib.Path("D:/Modding/MO2")
    game_path = pathlib.Path("D:/Steam/steamapps/common/Skyrim Special Edition")
    config_path = tmp_path / "config.toml"
    config_path.write_text('llm_provider = "anthropic"\n', encoding="utf-8")
    store = ReactiveStore()
    store.set(
        STORE_KEY_ENV,
        EnvironmentSnapshot(
            skyrim=SkyrimInfo(path=game_path, exe_name="SkyrimSE.exe", edition=SkyrimEdition.AE, version="1.6.1170"),
            mo2=MO2Info(path=mo2_root, profiles=["Default"]),
        ),
    )
    mods = [
        ModInstallResult(
            mod_name="Community Shaders",
            mod_dir=mo2_root / "mods" / "Community Shaders",
            version="v1.8.2",
            already_existed=False,
        )
    ]
    installer = _FakeInstaller(cs_result=mods)
    from types import SimpleNamespace

    ctx = _FakeAppContext(installer, network=SimpleNamespace(downloader="dl"), config_path=config_path)

    await run_ritual_install("community_shaders", app_context=ctx, store=store)

    assert installer.calls == [("ensure_community_shaders", mo2_root / "mods", "sess")]
    assert installer.cs_downloader == "dl"
    assert installer.cs_kwargs is not None
    assert installer.cs_kwargs["edition"] is SkyrimEdition.AE
    assert installer.cs_kwargs["game_version"] == "1.6.1170"
    assert installer.cs_kwargs["game_dir"] == game_path
    fb = store.get("ritual_feedback")
    assert fb is not None and fb["type"] == "positive"
    assert "Community Shaders" in fb["text"]
    assert not store.get("ritual_in_flight")


async def test_install_community_shaders_persiste_mods_en_config(tmp_path: pathlib.Path) -> None:
    """La GUI persiste community_shaders_mods (contrato NGIO: el orquestador activa).

    El `config_path` es un TOML **real**, no un JSON de conveniencia:
    `AppContext.config_path` es `Config.DEFAULT_CONFIG_FILE`
    (`~/.sky_claw/config.toml`) y con un fixture JSON este test pasaba en verde
    mientras producción reescribía el TOML del usuario con defaults. La segunda
    mitad de las aserciones —que lo que ya estaba sigue ahí— es el punto.
    """
    import tomllib

    from sky_claw.app.gui.views.forge_dashboard import STORE_KEY_ENV
    from sky_claw.local.discovery.environment import EnvironmentSnapshot, MO2Info, SkyrimEdition, SkyrimInfo
    from sky_claw.local.tools_installer import ModInstallResult

    mo2_root = tmp_path / "MO2"
    game_path = tmp_path / "Skyrim"
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        'llm_provider = "anthropic"\nmo2_root = "D:/Modding/MO2"\nskyrim_path = "D:/Skyrim"\n',
        encoding="utf-8",
    )
    store = ReactiveStore()
    store.set(
        STORE_KEY_ENV,
        EnvironmentSnapshot(
            skyrim=SkyrimInfo(path=game_path, exe_name="SkyrimSE.exe", edition=SkyrimEdition.SE, version="1.5.97"),
            mo2=MO2Info(path=mo2_root, profiles=["Default"]),
        ),
    )
    mods = [
        ModInstallResult(
            mod_name="Address Library for SKSE Plugins",
            mod_dir=mo2_root / "mods" / "Address Library for SKSE Plugins",
            version="v11",
            already_existed=False,
        )
    ]
    installer = _FakeInstaller(cs_result=mods)
    ctx = _FakeAppContext(installer, config_path=config_path)

    await run_ritual_install("community_shaders", app_context=ctx, store=store)

    persistido = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert persistido["community_shaders_mods"] == ["Address Library for SKSE Plugins"]
    # La config que ya existía sobrevive: persistir un campo no es reescribir todo.
    assert persistido["mo2_root"] == "D:/Modding/MO2"
    assert persistido["skyrim_path"] == "D:/Skyrim"
    assert persistido["llm_provider"] == "anthropic"


async def test_install_community_shaders_sin_mo2_devuelve_feedback_negativo() -> None:
    """Sin MO2 en el snapshot la rama corta antes de tocar el installer (misma
    fuente que la tarjeta: un sentinel sin mo2_root no se puede resolver)."""
    store = ReactiveStore()  # sin STORE_KEY_ENV → snapshot None
    installer = _FakeInstaller()
    ctx = _FakeAppContext(installer)

    await run_ritual_install("community_shaders", app_context=ctx, store=store)

    assert installer.calls == []
    fb = store.get("ritual_feedback")
    assert fb is not None and fb["type"] == "negative"
    assert "Mod Organizer" in fb["text"]


def _snapshot_cs(tmp_path: pathlib.Path):
    """Snapshot mínimo con MO2 + Skyrim AE válido para el install de CS."""
    from sky_claw.app.gui.views.forge_dashboard import STORE_KEY_ENV
    from sky_claw.local.discovery.environment import EnvironmentSnapshot, MO2Info, SkyrimEdition, SkyrimInfo

    mo2_root = tmp_path / "MO2"
    game_path = tmp_path / "Skyrim"
    snap = EnvironmentSnapshot(
        skyrim=SkyrimInfo(path=game_path, exe_name="SkyrimSE.exe", edition=SkyrimEdition.AE, version="1.6.1170"),
        mo2=MO2Info(path=mo2_root, profiles=["Default"]),
    )
    return mo2_root, game_path, snap, STORE_KEY_ENV


async def test_install_community_shaders_limpia_el_preflight_stale_de_un_ritual_previo(tmp_path: pathlib.Path) -> None:
    """run_ritual limpia ``STORE_KEY_RITUAL_PREFLIGHT`` al arrancar (solo LOOT lo
    produce). Su hermano run_ritual_install NO lo hacía: si un sort de LOOT dejaba
    un panel rojo y el operador instalaba Community Shaders, el panel rojo de LOOT
    seguía renderizándose, asociando visualmente la falla de LOOT al install de CS.
    Espejo exacto del clear de run_ritual (defecto #1 del repo: dos superficies).
    """
    mo2_root, _game, snap, key_env = _snapshot_cs(tmp_path)
    from sky_claw.local.tools_installer import ModInstallResult

    store = ReactiveStore()
    store.set(key_env, snap)
    # Preflight rojo remanente de un LOOT previo.
    store.set(STORE_KEY_RITUAL_PREFLIGHT, {"semaphore": "red", "checks": []})
    mods = [
        ModInstallResult(
            mod_name="Community Shaders",
            mod_dir=mo2_root / "mods" / "Community Shaders",
            version="v1.8.3",
            already_existed=False,
        )
    ]
    installer = _FakeInstaller(cs_result=mods)
    # Config real: el foco del test es el clear del preflight, no el camino
    # sin config_path (que tiene su propio test y degrada a warning).
    config_path = tmp_path / "config.toml"
    config_path.write_text('llm_provider = "anthropic"\n', encoding="utf-8")
    ctx = _FakeAppContext(installer, config_path=config_path)

    await run_ritual_install("community_shaders", app_context=ctx, store=store)

    assert store.get(STORE_KEY_RITUAL_PREFLIGHT) is None


async def test_install_community_shaders_avisa_si_falla_la_persistencia(tmp_path: pathlib.Path) -> None:
    """Si ``persistir_campo_bloqueante`` falla (config corrupta), el install en disco
    SÍ ocurrió pero el orquestador no verá los mods: reportarlo como ``positive``
    convierte una operación parcial en éxito visual (invariante GUI). Debe degradar
    a ``warning`` nombrando que la persistencia falló, sin perder el detalle de lo
    instalado. Y el fail-closed del lector deja el archivo corrupto intacto.
    """
    mo2_root, _game, snap, key_env = _snapshot_cs(tmp_path)
    from sky_claw.local.tools_installer import ModInstallResult

    config_path = tmp_path / "config.toml"
    config_path.write_text("[[[esto no es TOML válido", encoding="utf-8")

    store = ReactiveStore()
    store.set(key_env, snap)
    mods = [
        ModInstallResult(
            mod_name="Community Shaders",
            mod_dir=mo2_root / "mods" / "Community Shaders",
            version="v1.8.3",
            already_existed=False,
        )
    ]
    installer = _FakeInstaller(cs_result=mods)
    ctx = _FakeAppContext(installer, config_path=config_path)

    await run_ritual_install("community_shaders", app_context=ctx, store=store)

    fb = store.get("ritual_feedback")
    assert fb is not None
    assert fb["type"] == "warning"  # NO "positive": la persistencia falló
    assert "persist" in fb["text"].lower()
    # El fail-closed no reescribió la config corrupta.
    assert config_path.read_text(encoding="utf-8") == "[[[esto no es TOML válido"


async def test_install_community_shaders_sin_config_path_degrada_a_warning(tmp_path: pathlib.Path) -> None:
    """Hermano del caso con excepción (review adversarial del PR #445): sin
    ``config_path`` (boot incompleto), la persistencia NO puede correr — la
    operación también es parcial y el feedback debe decirlo. Cubrir solo el
    camino que lanza dejaba vivo el éxito visual que este fix vino a cerrar.
    """
    mo2_root, _game, snap, key_env = _snapshot_cs(tmp_path)
    from sky_claw.local.tools_installer import ModInstallResult

    store = ReactiveStore()
    store.set(key_env, snap)
    mods = [
        ModInstallResult(
            mod_name="Community Shaders",
            mod_dir=mo2_root / "mods" / "Community Shaders",
            version="v1.8.3",
            already_existed=False,
        )
    ]
    installer = _FakeInstaller(cs_result=mods)
    ctx = _FakeAppContext(installer, config_path=None)

    await run_ritual_install("community_shaders", app_context=ctx, store=store)

    fb = store.get("ritual_feedback")
    assert fb is not None
    assert fb["type"] == "warning"  # NO "positive": nada se persistió
    assert "persist" in fb["text"].lower()

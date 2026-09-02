"""Tests del análisis profundo de conflictos de records (F6 / PR6).

La lógica vive en ``RecordConflictScanner``
(``sky_claw/app/orchestrator/record_conflict_scan.py``);
``SupervisorAgent.scan_record_conflicts`` quedó como facade delegante para la GUI.
Cubre el seam puro de parseo de plugins, los contratos del scanner (perfil,
precedencia, fallback, guardas, construcción del runner, delegación al analyzer)
y la facade — todo sin construir un supervisor completo.
"""

from __future__ import annotations

import pathlib
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from sky_claw.app.orchestrator import record_conflict_scan as scan_module
from sky_claw.app.orchestrator.active_plugins import parse_active_plugins
from sky_claw.app.orchestrator.record_conflict_scan import (
    DEEP_SCAN_TIMEOUT_SECONDS,
    RecordConflictScanner,
)
from sky_claw.app.orchestrator.supervisor import SupervisorAgent
from sky_claw.local.xedit.conflict_analyzer import ConflictAnalyzer, ConflictReport


# ── parse_active_plugins (seam puro) ────────────────────────────────────────────
# Formato loadorder.txt (líneas simples) / plugins.txt (prefijo '*' = habilitado).
def test_toma_esp_esm_esl_en_orden() -> None:
    loadorder = "A.esp\nBase.esm\nDeshabilitado.esp\nLight.esl\n"
    assert parse_active_plugins(loadorder) == ["A.esp", "Base.esm", "Deshabilitado.esp", "Light.esl"]


def test_plugins_txt_conserva_solo_plugins_habilitados_con_asterisco() -> None:
    plugins_txt = "*Activo.esp\nBase.esm\n* Otro.esp\nDeshabilitado.esl\n"
    assert parse_active_plugins(plugins_txt, source="plugins_txt") == ["Activo.esp", "Otro.esp"]


def test_ignora_comentarios_y_no_plugins_y_hace_strip() -> None:
    text = "# comentario\nUn Mod Cualquiera\n Foo.esp \n\n  Bar.esm\n"
    assert parse_active_plugins(text) == ["Foo.esp", "Bar.esm"]


def test_archivo_vacio_devuelve_vacio() -> None:
    assert parse_active_plugins("") == []


# ── RecordConflictScanner ───────────────────────────────────────────────────────
def _scanner(path_resolver: Any, output_dir: pathlib.Path, **kwargs: Any) -> RecordConflictScanner:
    return RecordConflictScanner(path_resolver=path_resolver, output_dir=output_dir, **kwargs)


def _resolver_completo(tmp_path: pathlib.Path, **overrides: Any) -> SimpleNamespace:
    base = {
        "get_active_profile": lambda: "Default",
        "get_skyrim_path": lambda: tmp_path / "skyrim",
        "get_xedit_path": lambda: tmp_path / "xedit.exe",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _perfil_con(tmp_path: pathlib.Path, archivos: dict[str, str]) -> pathlib.Path:
    """Crea ``<tmp>/profiles/Default`` con los archivos de load order dados."""
    profile_dir = tmp_path / "profiles" / "Default"
    profile_dir.mkdir(parents=True, exist_ok=True)
    for nombre, contenido in archivos.items():
        (profile_dir / nombre).write_text(contenido, encoding="utf-8")
    return profile_dir


def _stub_analyzer(monkeypatch: pytest.MonkeyPatch, capturado: dict[str, Any], reporte: ConflictReport):
    async def _analyze(self: Any, plugins: Any, runner: Any) -> ConflictReport:
        capturado["llamado"] = True
        capturado["plugins"] = plugins
        capturado["runner"] = runner
        return reporte

    monkeypatch.setattr(ConflictAnalyzer, "analyze", _analyze)
    return capturado


# ── Grupo A: parsing y resolución de load order ─────────────────────────────────
async def test_perfil_explicito_vs_activo(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    """F1/F2: profile explícito manda; si es None usa get_active_profile()."""
    perfiles: list[str] = []
    _perfil_con(tmp_path, {"loadorder.txt": "A.esp\n"})
    _stub_analyzer(monkeypatch, {}, ConflictReport(total_conflicts=0, critical_conflicts=0))

    def _resolve(profile: str) -> pathlib.Path:
        perfiles.append(profile)
        return tmp_path / "profiles" / "Default" / "modlist.txt"

    resolver = _resolver_completo(tmp_path, resolve_modlist_path=_resolve, get_active_profile=lambda: "PerfilActivo")
    scanner = _scanner(resolver, tmp_path / "patches")

    # F1: explícito
    await scanner.scan(profile="PerfilX")
    assert perfiles == ["PerfilX"]

    # F2: fallback a get_active_profile()
    await scanner.scan()
    assert perfiles == ["PerfilX", "PerfilActivo"]


async def test_precedencia_loadorder_sobre_plugins_txt(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    """F4: loadorder.txt tiene precedencia sobre plugins.txt."""
    assert scan_module._LOAD_ORDER_CANDIDATES == (
        ("loadorder.txt", "loadorder"),
        ("plugins.txt", "plugins_txt"),
    )
    capturado = _stub_analyzer(monkeypatch, {}, ConflictReport(total_conflicts=0, critical_conflicts=0))
    profile_dir = _perfil_con(
        tmp_path,
        {"loadorder.txt": "DeLoadOrder.esp\n", "plugins.txt": "*DePluginsTxt.esp\n"},
    )
    scanner = _scanner(
        _resolver_completo(tmp_path, resolve_modlist_path=lambda p: profile_dir / "modlist.txt"),
        tmp_path / "patches",
    )

    await scanner.scan()
    assert capturado["plugins"] == ["DeLoadOrder.esp"]


@pytest.mark.parametrize(
    "loadorder_content",
    [None, "# solo comentarios\nUn Mod Cualquiera\n"],
    ids=["sin-loadorder", "loadorder-parsea-vacio"],
)
async def test_fallback_a_plugins_txt_si_no_hay_loadorder_o_parsea_vacio(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, loadorder_content: str | None
) -> None:
    """F5: si loadorder.txt no existe o parsea a [], cae a plugins.txt."""
    capturado = _stub_analyzer(monkeypatch, {}, ConflictReport(total_conflicts=0, critical_conflicts=0))
    archivos = {"plugins.txt": "*Rescatado.esp\n"}
    if loadorder_content is not None:
        archivos["loadorder.txt"] = loadorder_content
    profile_dir = _perfil_con(tmp_path, archivos)

    scanner = _scanner(
        _resolver_completo(tmp_path, resolve_modlist_path=lambda p: profile_dir / "modlist.txt"),
        tmp_path / "patches",
    )

    await scanner.scan()
    assert capturado["plugins"] == ["Rescatado.esp"]


async def test_source_correcto_por_archivo(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    """F6: loadorder usa source='loadorder'; plugins.txt usa source='plugins_txt' (asterisco obligatorio)."""
    capturado = _stub_analyzer(monkeypatch, {}, ConflictReport(total_conflicts=0, critical_conflicts=0))
    profile_dir = _perfil_con(tmp_path, {"loadorder.txt": "SinAsterisco.esp\n"})
    scanner = _scanner(
        _resolver_completo(tmp_path, resolve_modlist_path=lambda p: profile_dir / "modlist.txt"),
        tmp_path / "patches",
    )
    await scanner.scan()
    assert capturado["plugins"] == ["SinAsterisco.esp"]

    (profile_dir / "loadorder.txt").unlink()
    (profile_dir / "plugins.txt").write_text("*Habilitado.esp\nApagado.esp\n", encoding="utf-8")
    await scanner.scan()
    assert capturado["plugins"] == ["Habilitado.esp"]


# ── Grupo B: fast exits ─────────────────────────────────────────────────────────
async def test_plugins_explicitos_evitan_io(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    """F3: plugins explícitos evitan resolver modlist o leer load order del disco."""
    capturado = _stub_analyzer(monkeypatch, {}, ConflictReport(total_conflicts=0, critical_conflicts=0))

    def _explota(profile: str) -> pathlib.Path:
        pytest.fail("con plugins explícitos no se debe resolver modlist ni leer disco")

    scanner = _scanner(_resolver_completo(tmp_path, resolve_modlist_path=_explota), tmp_path / "patches")
    await scanner.scan(plugins=["Explicito.esp"])
    assert capturado["plugins"] == ["Explicito.esp"]


# R6 — source correcto por archivo
async def test_source_por_archivo_es_el_correcto(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    """El ``*`` solo filtra en ``plugins.txt``; en ``loadorder.txt`` no es marca.

    Si ``loadorder.txt`` se leyera con ``source="plugins_txt"`` sus líneas sin
    ``*`` desaparecerían; si ``plugins.txt`` se leyera como ``loadorder`` se
    colarían los deshabilitados y los nombres quedarían con el ``*`` pegado.
    """
    capturado = _stub_analyzer(monkeypatch, {}, ConflictReport(total_conflicts=0, critical_conflicts=0))
    profile_dir = _perfil_con(tmp_path, {"loadorder.txt": "SinAsterisco.esp\n"})
    scanner = _scanner(
        _resolver_completo(tmp_path, resolve_modlist_path=lambda p: profile_dir / "modlist.txt"),
        tmp_path / "patches",
    )
    await scanner.scan()
    assert capturado["plugins"] == ["SinAsterisco.esp"]

    (profile_dir / "loadorder.txt").unlink()
    (profile_dir / "plugins.txt").write_text("*Habilitado.esp\nApagado.esp\n", encoding="utf-8")
    await scanner.scan()
    assert capturado["plugins"] == ["Habilitado.esp"]


# R7 — cero plugins: reporte vacío, sin xEdit y sin resolver sus rutas
@pytest.mark.parametrize(
    "plugins",
    [[], None],
    ids=["lista-explicita-vacia", "perfil-sin-load-order"],
)
async def test_sin_plugins_devuelve_reporte_vacio_sin_correr_xedit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, plugins: list[str] | None
) -> None:
    capturado = _stub_analyzer(monkeypatch, {}, ConflictReport(total_conflicts=99, critical_conflicts=9))
    profile_dir = _perfil_con(tmp_path, {})
    scanner = _scanner(
        SimpleNamespace(
            get_active_profile=lambda: "Default",
            resolve_modlist_path=lambda p: profile_dir / "modlist.txt",
            get_skyrim_path=lambda: pytest.fail("no debe resolverse SKYRIM_PATH sin plugins"),
            get_xedit_path=lambda: pytest.fail("no debe resolverse XEDIT_PATH sin plugins"),
        ),
        tmp_path / "patches",
    )

    report = await scanner.scan(plugins=plugins)
    assert report.total_conflicts == 0
    assert report.critical_conflicts == 0
    assert capturado.get("llamado") is None


# ── Grupo C: xEdit y Runner ────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "skyrim,xedit",
    [(None, "xedit.exe"), ("skyrim", None), (None, None)],
    ids=["sin-skyrim", "sin-xedit", "sin-ninguna"],
)
async def test_sin_rutas_configuradas_lanza_runtime_error(
    tmp_path: pathlib.Path, skyrim: str | None, xedit: str | None
) -> None:
    """F8: sin Skyrim o xEdit paths se preserva el RuntimeError histórico."""
    scanner = _scanner(
        SimpleNamespace(
            get_active_profile=lambda: "Default",
            get_skyrim_path=lambda: (tmp_path / skyrim) if skyrim else None,
            get_xedit_path=lambda: (tmp_path / xedit) if xedit else None,
        ),
        tmp_path / "patches",
    )
    with pytest.raises(RuntimeError, match="SKYRIM_PATH y XEDIT_PATH"):
        await scanner.scan(plugins=["A.esp", "B.esp"])


async def test_runner_recibe_rutas_output_dir_y_timeout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """F9: runner recibe xedit_path, game_path, output_dir y timeout; output_dir relativo queda relativo."""
    from sky_claw.local.xedit.runner import XEditRunner

    monkeypatch.chdir(tmp_path)
    capturado = _stub_analyzer(monkeypatch, {}, ConflictReport(total_conflicts=0, critical_conflicts=0))
    relativo = pathlib.Path(".skyclaw_backups/") / "patches"
    scanner = _scanner(_resolver_completo(tmp_path), relativo)

    await scanner.scan(plugins=["A.esp"])

    runner = capturado["runner"]
    assert isinstance(runner, XEditRunner)
    assert runner._xedit_path == tmp_path / "xedit.exe"
    assert runner._game_path == tmp_path / "skyrim"
    assert runner._output_dir == relativo
    assert not runner._output_dir.is_absolute()
    assert (tmp_path / ".skyclaw_backups" / "patches").is_dir()
    assert runner._timeout == DEEP_SCAN_TIMEOUT_SECONDS
    assert DEEP_SCAN_TIMEOUT_SECONDS == 900


async def test_delega_en_analyzer_preservando_orden_e_identidad(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """F10/F11: analyzer recibe plugins en el orden exacto de entrada y retorna identidad."""
    esperado = ConflictReport(total_conflicts=3, critical_conflicts=1)
    capturado = _stub_analyzer(monkeypatch, {}, esperado)
    scanner = _scanner(_resolver_completo(tmp_path), tmp_path / "patches")

    # Orden NO alfabético a propósito
    entrada = ["Zeta.esp", "Alpha.esp", "Medio.esm"]
    report = await scanner.scan(plugins=list(entrada))

    assert capturado["plugins"] == entrada
    assert report is esperado


# ── Grupo D: Façade del Supervisor ─────────────────────────────────────────────
async def test_facade_del_supervisor_delega_en_el_scanner() -> None:
    """SupervisorAgent.scan_record_conflicts delega en scanner.scan propagando argumentos."""
    supervisor = SupervisorAgent.__new__(SupervisorAgent)
    scanner = MagicMock()
    esperado = ConflictReport(total_conflicts=7, critical_conflicts=2)
    scanner.scan = AsyncMock(return_value=esperado)
    supervisor._record_conflict_scanner = scanner

    # Llamada explícita
    report = await supervisor.scan_record_conflicts(profile="PerfilX", plugins=["A.esp"])
    assert report is esperado
    scanner.scan.assert_awaited_once_with(profile="PerfilX", plugins=["A.esp"])

    # Llamada default: pasa None/None
    scanner.scan.reset_mock()
    await supervisor.scan_record_conflicts()
    scanner.scan.assert_awaited_once_with(profile=None, plugins=None)

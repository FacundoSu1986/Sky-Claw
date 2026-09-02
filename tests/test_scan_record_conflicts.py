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

from sky_claw.app.orchestrator import plugin_limit_guard as guard_module
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


# ── Ancla del hermano: las DOS políticas de precedencia, por igualdad literal ───
def test_las_politicas_de_precedencia_hermanas_no_se_unifican() -> None:
    """Congela AMBOS órdenes de candidatos (enumera, no muestrea).

    ``RecordConflictScanner`` y ``PluginLimitGuard`` leen los mismos dos archivos
    con políticas OPUESTAS a propósito: el scanner prueba ``loadorder.txt``
    primero y sigue al siguiente candidato si parsea a ``[]``; el guard prueba
    ``plugins.txt`` primero y deja que la lista vacía gane. Un futuro
    "unifiquemos los dos readers" rompe acá — en vez de cambiar en silencio la
    política de uno solo de los caminos (la clase de defecto dominante del repo).
    """
    assert scan_module._LOAD_ORDER_CANDIDATES == (
        ("loadorder.txt", "loadorder"),
        ("plugins.txt", "plugins_txt"),
    )
    assert guard_module._LOAD_ORDER_CANDIDATES == (
        ("plugins.txt", "plugins_txt"),
        ("loadorder.txt", "loadorder"),
    )
    espejo_del_guard = tuple(reversed(guard_module._LOAD_ORDER_CANDIDATES))
    assert espejo_del_guard == scan_module._LOAD_ORDER_CANDIDATES, (
        "las dos políticas dejaron de ser espejo: revisá que el cambio sea deliberado en AMBOS caminos"
    )


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


# R1/R2 — perfil explícito vs. perfil activo
async def test_perfil_explicito_se_usa_tal_cual(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    perfiles: list[str] = []
    _perfil_con(tmp_path, {"loadorder.txt": "A.esp\n"})
    _stub_analyzer(monkeypatch, {}, ConflictReport(total_conflicts=0, critical_conflicts=0))

    def _resolve(profile: str) -> pathlib.Path:
        perfiles.append(profile)
        return tmp_path / "profiles" / "Default" / "modlist.txt"

    scanner = _scanner(
        _resolver_completo(
            tmp_path,
            resolve_modlist_path=_resolve,
            get_active_profile=lambda: pytest.fail("no debe consultarse el perfil activo"),
        ),
        tmp_path / "patches",
    )

    await scanner.scan(profile="PerfilX")
    assert perfiles == ["PerfilX"]


async def test_sin_perfil_usa_el_perfil_activo_del_resolver(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    perfiles: list[str] = []
    _perfil_con(tmp_path, {"loadorder.txt": "A.esp\n"})
    _stub_analyzer(monkeypatch, {}, ConflictReport(total_conflicts=0, critical_conflicts=0))

    def _resolve(profile: str) -> pathlib.Path:
        perfiles.append(profile)
        return tmp_path / "profiles" / "Default" / "modlist.txt"

    scanner = _scanner(
        _resolver_completo(tmp_path, resolve_modlist_path=_resolve, get_active_profile=lambda: "ActivoDelResolver"),
        tmp_path / "patches",
    )

    await scanner.scan()
    assert perfiles == ["ActivoDelResolver"]


# R3 — plugins explícitos: cero I/O de load order
async def test_plugins_explicitos_no_leen_el_load_order(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    capturado = _stub_analyzer(monkeypatch, {}, ConflictReport(total_conflicts=0, critical_conflicts=0))
    _perfil_con(tmp_path, {"loadorder.txt": "DelDisco.esp\n", "plugins.txt": "*DelDisco.esp\n"})

    def _explota(profile: str) -> pathlib.Path:
        pytest.fail("con plugins explícitos no se debe resolver el modlist ni leer el load order")

    scanner = _scanner(_resolver_completo(tmp_path, resolve_modlist_path=_explota), tmp_path / "patches")

    await scanner.scan(plugins=["A.esp"])
    assert capturado["plugins"] == ["A.esp"]


# R4 — precedencia: loadorder.txt gana sobre plugins.txt
async def test_loadorder_tiene_precedencia_sobre_plugins_txt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
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


async def test_lee_plugins_del_loadorder_cuando_no_se_pasan(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    # El load order de plugins vive en loadorder.txt (sibling de modlist.txt en
    # el dir del perfil), NO en modlist.txt (que lista mods) — review Copilot #226.
    capturado = _stub_analyzer(monkeypatch, {}, ConflictReport(total_conflicts=0, critical_conflicts=0))
    profile_dir = _perfil_con(tmp_path, {"loadorder.txt": "A.esp\nBase.esm\n"})
    scanner = _scanner(
        _resolver_completo(tmp_path, resolve_modlist_path=lambda p: profile_dir / "modlist.txt"),
        tmp_path / "patches",
    )

    await scanner.scan()
    assert capturado["plugins"] == ["A.esp", "Base.esm"]


async def test_fallback_a_plugins_txt_si_no_hay_loadorder(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    capturado = _stub_analyzer(monkeypatch, {}, ConflictReport(total_conflicts=0, critical_conflicts=0))
    profile_dir = _perfil_con(
        tmp_path,
        {"plugins.txt": "*A.esp\nBase.esm\n*Base.esm\nDeshabilitado.esl\n* Light.esl\n"},
    )
    scanner = _scanner(
        _resolver_completo(tmp_path, resolve_modlist_path=lambda p: profile_dir / "modlist.txt"),
        tmp_path / "patches",
    )

    await scanner.scan()
    assert capturado["plugins"] == ["A.esp", "Base.esm", "Light.esl"]


# R5 — la regla que separa esta política de la del PluginLimitGuard
async def test_loadorder_que_parsea_vacio_cae_a_plugins_txt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """``loadorder.txt`` EXISTE pero no produce plugins ⇒ se prueba ``plugins.txt``.

    Regla histórica de record-conflicts, OPUESTA a la del guard M-04 (donde la
    lista vacía del primer candidato gana). Un ``loadorder.txt`` con solo
    comentarios/mods es exactamente el caso que la haría notar.
    """
    capturado = _stub_analyzer(monkeypatch, {}, ConflictReport(total_conflicts=0, critical_conflicts=0))
    profile_dir = _perfil_con(
        tmp_path,
        {"loadorder.txt": "# solo comentarios\nUn Mod Cualquiera\n", "plugins.txt": "*Rescatado.esp\n"},
    )
    scanner = _scanner(
        _resolver_completo(tmp_path, resolve_modlist_path=lambda p: profile_dir / "modlist.txt"),
        tmp_path / "patches",
    )

    await scanner.scan()
    assert capturado["plugins"] == ["Rescatado.esp"]


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


# R8 — rutas obligatorias
@pytest.mark.parametrize(
    "skyrim,xedit",
    [(None, "xedit.exe"), ("skyrim", None), (None, None)],
    ids=["sin-skyrim", "sin-xedit", "sin-ninguna"],
)
async def test_sin_rutas_configuradas_lanza(tmp_path: pathlib.Path, skyrim: str | None, xedit: str | None) -> None:
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


# R9/R10/R11 — el runner recibe rutas, output_dir y timeout exactos
async def test_el_runner_recibe_rutas_output_dir_y_timeout_exactos(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """El escaneo profundo debe darle a xEdit mucho más que el default de 120s (Codex #226)."""
    capturado = _stub_analyzer(monkeypatch, {}, ConflictReport(total_conflicts=0, critical_conflicts=0))
    output_dir = tmp_path / "staging" / "patches"
    scanner = _scanner(_resolver_completo(tmp_path), output_dir)

    await scanner.scan(plugins=["A.esp"])

    runner = capturado["runner"]
    assert runner._xedit_path == tmp_path / "xedit.exe"
    assert runner._game_path == tmp_path / "skyrim"
    assert runner._output_dir == output_dir
    assert runner._timeout == DEEP_SCAN_TIMEOUT_SECONDS
    assert DEEP_SCAN_TIMEOUT_SECONDS == 900
    assert DEEP_SCAN_TIMEOUT_SECONDS > 120


async def test_el_output_dir_relativo_se_conserva_relativo(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """Un ``output_dir`` relativo NO se ancla al cwd de construcción.

    El wiring inyecta ``pathlib.Path(BACKUP_STAGING_DIR) / "patches"``, que es
    relativo: ``XEditRunner`` lo resuelve contra el cwd del scan (crea el dir
    ahí). Si el scanner lo normalizara con ``.resolve()``/``.absolute()`` los
    patches caerían en otro lado.
    """
    monkeypatch.chdir(tmp_path)
    capturado = _stub_analyzer(monkeypatch, {}, ConflictReport(total_conflicts=0, critical_conflicts=0))
    relativo = pathlib.Path(".skyclaw_backups/") / "patches"
    scanner = _scanner(_resolver_completo(tmp_path), relativo)

    await scanner.scan(plugins=["A.esp"])

    assert capturado["runner"]._output_dir == relativo
    assert not capturado["runner"]._output_dir.is_absolute()
    assert (tmp_path / ".skyclaw_backups" / "patches").is_dir()


async def test_el_timeout_inyectado_manda_sobre_el_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    capturado = _stub_analyzer(monkeypatch, {}, ConflictReport(total_conflicts=0, critical_conflicts=0))
    scanner = _scanner(_resolver_completo(tmp_path), tmp_path / "patches", timeout=77)

    await scanner.scan(plugins=["A.esp"])
    assert capturado["runner"]._timeout == 77


# R12/R13/R14 — delegación al analyzer
async def test_delega_en_el_analyzer_con_los_plugins_en_el_mismo_orden(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    esperado = ConflictReport(total_conflicts=3, critical_conflicts=1)
    capturado = _stub_analyzer(monkeypatch, {}, esperado)
    scanner = _scanner(_resolver_completo(tmp_path), tmp_path / "patches")

    # Orden NO alfabético a propósito: un sorted() intermedio lo delataría.
    entrada = ["Zeta.esp", "Alpha.esp", "Medio.esm"]
    report = await scanner.scan(plugins=list(entrada))

    assert capturado["plugins"] == entrada
    assert report is esperado


async def test_el_runner_que_llega_al_analyzer_es_el_construido(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    from sky_claw.local.xedit.runner import XEditRunner

    capturado = _stub_analyzer(monkeypatch, {}, ConflictReport(total_conflicts=0, critical_conflicts=0))
    scanner = _scanner(_resolver_completo(tmp_path), tmp_path / "patches")

    await scanner.scan(plugins=["A.esp"])
    assert isinstance(capturado["runner"], XEditRunner)


# ── Facade del supervisor (compatibilidad pública con la GUI) ───────────────────
async def test_la_facade_del_supervisor_delega_en_el_scanner() -> None:
    """``SupervisorAgent.scan_record_conflicts`` sigue existiendo y delega.

    La GUI llama ``runtime.supervisor.scan_record_conflicts()``
    (``sky_claw/app/gui/sky_claw_gui.py``), así que la facade es contrato público.
    """
    supervisor = SupervisorAgent.__new__(SupervisorAgent)
    scanner = MagicMock()
    esperado = ConflictReport(total_conflicts=7, critical_conflicts=2)
    scanner.scan = AsyncMock(return_value=esperado)
    supervisor._record_conflict_scanner = scanner

    report = await supervisor.scan_record_conflicts(profile="PerfilX", plugins=["A.esp"])

    assert report is esperado
    scanner.scan.assert_awaited_once_with(profile="PerfilX", plugins=["A.esp"])


async def test_la_facade_propaga_los_defaults_sin_reinterpretarlos() -> None:
    """Sin argumentos, la facade pasa ``None``/``None`` — no inventa un perfil."""
    supervisor = SupervisorAgent.__new__(SupervisorAgent)
    scanner = MagicMock()
    scanner.scan = AsyncMock(return_value=ConflictReport(total_conflicts=0, critical_conflicts=0))
    supervisor._record_conflict_scanner = scanner

    await supervisor.scan_record_conflicts()

    scanner.scan.assert_awaited_once_with(profile=None, plugins=None)

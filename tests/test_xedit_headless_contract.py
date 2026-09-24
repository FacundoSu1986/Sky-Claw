"""Contrato de ejecución headless de xEdit y de la API Pascal bundleada.

Historia (análisis de la herramienta xEdit, 2026-09): la capa Python estaba
bien planteada pero el borde con xEdit no funcionaba en un rig real:

- ``run_script`` y el comando de escritura no pasaban ``-autoexit`` (xEdit
  queda abierto hasta el timeout — ver "What's new 4.0.2 / Auto Exit") y
  apuntaban ``-D:`` a la RAÍZ del juego en vez de a ``Data`` (sus hermanos
  ``quick_auto_clean`` y ``launch_interactive`` ya usaban ``Data``). Al comando
  de escritura además le faltaba ``-autoload`` (diálogo de selección).
- xEdit es un binario GUI: ``AddMessage`` va al log (``-R:<archivo>``), no a
  stdout. Todo el protocolo ``CONFLICT|``/``SUMMARY|`` se leía de un stdout
  vacío y el análisis podía reportar "0 conflictos" como éxito.
- Los ``.pas`` usaban ``AddNewFile(nombre)`` (la API no recibe nombre: abre un
  diálogo — la correcta es ``AddNewFileName``), ``wbCopyElementToRecord`` para
  copiar un record a un archivo (copia un subelemento DENTRO de un record — la
  correcta es ``wbCopyElementToFile``) sin ``AddRequiredElementMasters``, y
  variables locales con el nombre de funciones de xEdit (``formID := FormID(e)``:
  Pascal no distingue mayúsculas, la local oculta a la función).

Estos tests ENUMERAN (regla de AGENTS.md): la familia de lanzadores headless se
detecta por AST y se congela; los chequeos de API recorren TODOS los scripts
bundleados y TODOS los templates generados, no un caso escrito a mano.
xEdit no corre en CI: el smoke real sigue siendo necesario (ver el header de
cada ``.pas``); lo que se ancla acá es que el repo no emita lo que la
documentación oficial de xEdit dice que no funciona.
"""

from __future__ import annotations

import ast
import inspect
import pathlib
import re
import textwrap

import pytest

import sky_claw.local.xedit
from sky_claw.local.xedit import runner as runner_mod
from sky_claw.local.xedit.conflict_analyzer import ConflictAnalyzer
from sky_claw.local.xedit.patch_orchestrator import PatchPlan, PatchStrategyType
from sky_claw.local.xedit.runner import ScriptGenerator, XEditRunner, XEditWriteError

_SCRIPTS_DIR = pathlib.Path(sky_claw.local.xedit.__file__).parent / "scripts"


# =============================================================================
# Helpers
# =============================================================================


def _runner(tmp_path: pathlib.Path) -> tuple[XEditRunner, pathlib.Path]:
    xedit = tmp_path / "SSEEdit.exe"
    xedit.touch()
    (tmp_path / "Edit Scripts").mkdir()
    (tmp_path / "Edit Scripts" / "mteFunctions.pas").write_text("unit mteFunctions;", encoding="utf-8")
    game = tmp_path / "Skyrim"
    (game / "Data").mkdir(parents=True)
    return XEditRunner(xedit_path=xedit, game_path=game, output_dir=tmp_path / "out"), game


def _log_arg(args: list[str]) -> pathlib.Path:
    logs = [a for a in args if a.startswith("-R:")]
    assert len(logs) == 1, f"se esperaba exactamente un -R:<log>, hay {logs}"
    return pathlib.Path(logs[0][3:])


def _fake_run_capture(capturado: dict[str, list[str]], log_text: str | None, stdout: bytes = b""):
    """Simula xEdit GUI: stdout vacío y los AddMessage en el archivo de ``-R:``."""

    async def fake(args: list[str], timeout: float | None = None, cwd: str | None = None):
        capturado["args"] = list(args)
        if log_text is not None:
            _log_arg(args).write_text(log_text, encoding="utf-8")
        return (stdout, b"", 0)

    return fake


def _metodos_que_lanzan_xedit() -> set[str]:
    """Métodos de XEditRunner que llaman a ``self._execute_process`` (por AST)."""
    fuente = textwrap.dedent(inspect.getsource(XEditRunner))
    arbol = ast.parse(fuente)
    clase = arbol.body[0]
    assert isinstance(clase, ast.ClassDef)
    encontrados: set[str] = set()
    for nodo in clase.body:
        if not isinstance(nodo, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for sub in ast.walk(nodo):
            if (
                isinstance(sub, ast.Call)
                and isinstance(sub.func, ast.Attribute)
                and sub.func.attr in {"_execute_process", "_execute_headless"}
                and nodo.name not in {"_execute_process", "_execute_headless"}
            ):
                encontrados.add(nodo.name)
    return encontrados


# =============================================================================
# Familia de lanzadores headless (enumerada)
# =============================================================================

#: Lanzadores headless que leen el protocolo AddMessage desde el log ``-R:``.
_LANZADORES_CON_LOG = {"run_script", "run_dynamic_script"}

#: Exención declarada: QuickAutoClean no emite protocolo AddMessage propio y su
#: veredicto es el exit code + stdout (validado en rig). Sumarle el log completo
#: de xEdit al parser de errores no anclado cambiaría su criterio de éxito sin
#: evidencia de rig — queda fuera a propósito y se revisa en su propio PR.
_LANZADORES_SIN_LOG = {"quick_auto_clean"}


def test_la_familia_de_lanzadores_headless_esta_congelada() -> None:
    """Un lanzador nuevo rompe el ancla hasta que se decide si lee el log."""
    detector = _metodos_que_lanzan_xedit()
    assert detector, "el detector no encontró ningún lanzador: el ancla no puede pasar por no ver nada"
    assert detector == _LANZADORES_CON_LOG | _LANZADORES_SIN_LOG


async def _argv_de(
    nombre: str, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[list[str], XEditRunner]:
    runner, _ = _runner(tmp_path)
    capturado: dict[str, list[str]] = {}
    # QuickAutoClean no pide -R: (exención declarada arriba): no hay log que escribir.
    log = "SUMMARY|total_conflicts=0|critical=0|minor=0\n" if nombre in _LANZADORES_CON_LOG else None
    monkeypatch.setattr(runner_mod, "run_capture", _fake_run_capture(capturado, log))
    if nombre == "run_script":
        await runner.run_script("list_all_conflicts.pas", ["Skyrim.esm"])
    elif nombre == "run_dynamic_script":
        await runner.run_dynamic_script("unit x; end.", ["Skyrim.esm"], flags=["-IKnowWhatImDoing"])
    elif nombre == "quick_auto_clean":
        await runner.quick_auto_clean("Update.esm")
    else:  # pragma: no cover - el ancla de familia obliga a agregar la receta
        raise AssertionError(f"lanzador sin receta de test: {nombre}")
    return capturado["args"], runner


@pytest.mark.parametrize("nombre", sorted(_LANZADORES_CON_LOG | _LANZADORES_SIN_LOG))
async def test_todo_lanzador_headless_cierra_solo_y_apunta_a_data(
    nombre: str, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    args, _ = await _argv_de(nombre, tmp_path, monkeypatch)
    game = tmp_path / "Skyrim"
    assert "-autoexit" in args, f"{nombre}: sin -autoexit xEdit queda abierto hasta el timeout"
    assert "-autoload" in args, f"{nombre}: sin -autoload aparece el diálogo de selección de plugins"
    assert f"-D:{game / 'Data'}" in args, f"{nombre}: -D: debe apuntar a Data, no a la raíz del juego"
    assert f"-D:{game}" not in args


@pytest.mark.parametrize("nombre", sorted(_LANZADORES_CON_LOG))
async def test_lanzadores_con_protocolo_piden_log_y_lo_limpian(
    nombre: str, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    args, _ = await _argv_de(nombre, tmp_path, monkeypatch)
    log = _log_arg(args)
    assert log.parent == tmp_path / "out", "el log debe vivir en el output_dir del runner"
    assert not log.exists(), "el log temporal debe borrarse tras leerlo (crecimiento de disco no acotado)"


def test_el_comando_de_escritura_es_headless_completo(tmp_path: pathlib.Path) -> None:
    runner, game = _runner(tmp_path)
    cmd = runner._build_write_command(tmp_path / "x.pas", ["Skyrim.esm"], ["-IKnowWhatImDoing"])
    assert "-autoexit" in cmd
    assert "-autoload" in cmd
    assert f"-D:{game / 'Data'}" in cmd


# =============================================================================
# El protocolo se lee del log (xEdit GUI no escribe stdout)
# =============================================================================


async def test_run_script_lee_el_protocolo_del_log_con_timestamps(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, _ = _runner(tmp_path)
    log = (
        "[00:00] Background Loader: finished\n"
        "[00:02] CONFLICT|00012345|EdId|NPC_|B.esp|A.esp\n"
        "SUMMARY|total_conflicts=1|critical=1|minor=0\n"
    )
    capturado: dict[str, list[str]] = {}
    monkeypatch.setattr(runner_mod, "run_capture", _fake_run_capture(capturado, log))

    result = await runner.run_script("list_all_conflicts.pas", ["A.esp", "B.esp"])

    lineas = result.raw_stdout.splitlines()
    assert "CONFLICT|00012345|EdId|NPC_|B.esp|A.esp" in lineas, "el timestamp debe quitarse de las líneas de protocolo"
    assert "SUMMARY|total_conflicts=1|critical=1|minor=0" in lineas
    # Las líneas que no son de protocolo conservan su timestamp (parsers legacy lo esperan).
    assert "[00:00] Background Loader: finished" in lineas


async def test_run_script_no_duplica_si_stdout_y_log_traen_lo_mismo(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, _ = _runner(tmp_path)
    linea = "CONFLICT|00012345|EdId|NPC_|B.esp|A.esp"
    capturado: dict[str, list[str]] = {}
    monkeypatch.setattr(
        runner_mod, "run_capture", _fake_run_capture(capturado, linea + "\n", stdout=(linea + "\n").encode())
    )
    result = await runner.run_script("list_all_conflicts.pas", ["A.esp", "B.esp"])
    assert result.raw_stdout.count(linea) == 1


async def test_analisis_sin_summary_falla_cerrado(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Exit 0 sin salida de protocolo NO es "0 conflictos": el script no llegó a Finalize."""
    runner, _ = _runner(tmp_path)
    capturado: dict[str, list[str]] = {}
    monkeypatch.setattr(runner_mod, "run_capture", _fake_run_capture(capturado, None))

    async def _staging_noop(_nombres: object) -> list[object]:
        return []

    monkeypatch.setattr(runner, "ensure_scripts_staged", _staging_noop)
    with pytest.raises(RuntimeError, match="SUMMARY"):
        await ConflictAnalyzer().analyze(["Skyrim.esm"], runner)


async def test_analisis_con_summary_inconsistente_falla_cerrado(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, _ = _runner(tmp_path)
    log = "CONFLICT|00012345|EdId|NPC_|B.esp|A.esp\nSUMMARY|total_conflicts=2|critical=2|minor=0\n"
    capturado: dict[str, list[str]] = {}
    monkeypatch.setattr(runner_mod, "run_capture", _fake_run_capture(capturado, log))

    async def _staging_noop(_nombres: object) -> list[object]:
        return []

    monkeypatch.setattr(runner, "ensure_scripts_staged", _staging_noop)
    with pytest.raises(RuntimeError, match="inconsistente"):
        await ConflictAnalyzer().analyze(["A.esp", "B.esp"], runner)


async def test_analisis_con_summary_consistente_devuelve_reporte(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, _ = _runner(tmp_path)
    log = "[00:03] CONFLICT|00012345|EdId|NPC_|B.esp|A.esp\n[00:03] SUMMARY|total_conflicts=1|critical=1|minor=0\n"
    capturado: dict[str, list[str]] = {}
    monkeypatch.setattr(runner_mod, "run_capture", _fake_run_capture(capturado, log))

    async def _staging_noop(_nombres: object) -> list[object]:
        return []

    monkeypatch.setattr(runner, "ensure_scripts_staged", _staging_noop)
    report = await ConflictAnalyzer().analyze(["A.esp", "B.esp"], runner)
    assert report.total_conflicts == 1
    assert report.critical_conflicts == 1


# =============================================================================
# Post-check de escritura: exit 0 no prueba que el .esp exista
# =============================================================================


async def test_execute_patch_falla_si_el_plugin_de_salida_no_quedo_en_data(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, _ = _runner(tmp_path)
    script = tmp_path / "fix.pas"
    script.write_text("unit fix; end.", encoding="utf-8")
    plan = PatchPlan(
        strategy_type=PatchStrategyType.EXECUTE_XEDIT_SCRIPT,
        target_plugins=["A.esp"],
        output_plugin="SkyClaw_CriticalPatch.esp",
        form_ids=["00012345"],
        estimated_records=1,
        requires_hitl=True,
        script_path=script,
    )
    capturado: dict[str, list[str]] = {}
    monkeypatch.setattr(runner_mod, "run_capture", _fake_run_capture(capturado, "Done\n"))

    with pytest.raises(XEditWriteError, match="SkyClaw_CriticalPatch.esp"):
        await runner.execute_patch(plan)


async def test_execute_patch_ok_si_el_plugin_de_salida_existe(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, game = _runner(tmp_path)
    script = tmp_path / "fix.pas"
    script.write_text("unit fix; end.", encoding="utf-8")
    plan = PatchPlan(
        strategy_type=PatchStrategyType.EXECUTE_XEDIT_SCRIPT,
        target_plugins=["A.esp"],
        output_plugin="SkyClaw_CriticalPatch.esp",
        form_ids=["00012345"],
        estimated_records=1,
        requires_hitl=True,
        script_path=script,
    )
    (game / "Data" / "SkyClaw_CriticalPatch.esp").write_bytes(b"TES4")
    capturado: dict[str, list[str]] = {}
    monkeypatch.setattr(runner_mod, "run_capture", _fake_run_capture(capturado, "Done\n"))

    result = await runner.execute_patch(plan)
    assert result.success is True


# =============================================================================
# API Pascal: todos los scripts bundleados + todos los templates generados
# =============================================================================


def _scripts_pascal() -> dict[str, str]:
    """Universo completo: .pas bundleados + cada template de ScriptGenerator."""
    universo = {p.name: p.read_text(encoding="utf-8") for p in sorted(_SCRIPTS_DIR.glob("*.pas"))}
    assert len(universo) >= 5, "el detector de scripts bundleados no encontró nada"
    templates = {n for n in vars(ScriptGenerator) if n.startswith("TEMPLATE_")}
    assert templates == {"TEMPLATE_FORWARD_RECORD", "TEMPLATE_MERGE_LEVELED_LIST", "TEMPLATE_APPLY_PATCH"}, (
        "template nuevo: agregá su receta de generación acá para que entre al chequeo de API"
    )
    universo["<template forward>"] = ScriptGenerator.generate_forward_script("00012345", "A.esp", "Out.esp")
    universo["<template merge>"] = ScriptGenerator.generate_merge_script("Out.esp", ["LVLI"])
    universo["<template apply>"] = ScriptGenerator.generate_patch_script("Out.esp", ["NPC_"], ["00012345"])
    return universo


def _sin_comentarios_ni_strings(fuente: str) -> str:
    fuente = re.sub(r"\{.*?\}", " ", fuente, flags=re.DOTALL)
    fuente = re.sub(r"\(\*.*?\*\)", " ", fuente, flags=re.DOTALL)
    fuente = re.sub(r"//[^\n]*", " ", fuente)
    return re.sub(r"'[^']*'", "''", fuente)


def _identificadores_declarados(fuente: str) -> set[str]:
    """Locales (bloques ``var``) y parámetros de rutinas, en minúsculas."""
    declarados: set[str] = set()
    for bloque in re.findall(r"\bvar\b(.*?)\bbegin\b", fuente, flags=re.DOTALL | re.IGNORECASE):
        for m in re.finditer(r"([\w\s,]+?)\s*:\s*[^;=]+;", bloque):
            declarados.update(n.strip().lower() for n in m.group(1).split(",") if n.strip())
    for params in re.findall(r"\b(?:function|procedure)\s+\w+\s*\(([^)]*)\)", fuente, flags=re.IGNORECASE):
        for grupo in params.split(";"):
            if ":" in grupo:
                nombres = grupo.split(":", 1)[0].replace("var ", "").replace("const ", "")
                declarados.update(n.strip().lower() for n in nombres.split(",") if n.strip())
    return declarados


def test_el_detector_de_sombreado_ve_el_caso_real() -> None:
    """Sanidad del detector: el patrón que existía en el repo debe detectarse."""
    fuente = "function Process(e: IInterface): Integer;\nvar\n  formID: string;\nbegin\n  formID := FormID(e);\nend;"
    declarados = _identificadores_declarados(_sin_comentarios_ni_strings(fuente))
    llamados = {m.lower() for m in re.findall(r"\b(\w+)\s*\(", fuente)}
    assert "formid" in declarados & llamados


@pytest.mark.parametrize("nombre", sorted(_scripts_pascal()))
def test_ninguna_local_oculta_una_funcion_llamada(nombre: str) -> None:
    fuente = _sin_comentarios_ni_strings(_scripts_pascal()[nombre])
    llamados = {m.lower() for m in re.findall(r"\b(\w+)\s*\(", fuente)}
    sombreados = _identificadores_declarados(fuente) & llamados
    assert not sombreados, (
        f"{nombre}: {sorted(sombreados)} se declaran como variable/parámetro y también se llaman como "
        "función; Pascal no distingue mayúsculas y la local oculta a la función de xEdit."
    )


@pytest.mark.parametrize("nombre", sorted(_scripts_pascal()))
def test_api_de_escritura_correcta(nombre: str) -> None:
    fuente = _sin_comentarios_ni_strings(_scripts_pascal()[nombre])
    assert not re.search(r"\bAddNewFile\s*\(", fuente), (
        f"{nombre}: AddNewFile no recibe nombre (abre un diálogo); usar AddNewFileName(nombre, False)"
    )
    assert "wbCopyElementToRecord" not in fuente, (
        f"{nombre}: wbCopyElementToRecord copia un subelemento dentro de un record; "
        "para copiar un record a un plugin usar wbCopyElementToFile"
    )
    for rutina in re.split(r"\b(?:function|procedure)\b", fuente, flags=re.IGNORECASE):
        if "wbCopyElementToFile" in rutina:
            assert rutina.find("AddRequiredElementMasters") != -1, (
                f"{nombre}: wbCopyElementToFile sin AddRequiredElementMasters previo en la misma rutina"
            )
            assert rutina.find("AddRequiredElementMasters") < rutina.find("wbCopyElementToFile")


def test_el_merge_estatico_no_toma_el_output_de_la_linea_de_comandos() -> None:
    """ParamStr ve también los plugins cargados: el primero '.es*' era un plugin
    FUENTE, y el script escribía los records dentro de ese mod."""
    fuente = _sin_comentarios_ni_strings((_SCRIPTS_DIR / "apply_leveled_list_merge.pas").read_text(encoding="utf-8"))
    assert "ParamStr" not in fuente
    assert "ParamCount" not in fuente


def test_list_all_conflicts_descarta_itm_y_benignos() -> None:
    """Mismo filtro que el script oficial de xEdit "Detect conflict between elements"."""
    fuente = _sin_comentarios_ni_strings((_SCRIPTS_DIR / "list_all_conflicts.pas").read_text(encoding="utf-8"))
    assert re.search(r"ConflictAllForMainRecord\(e\)\s*<\s*caOverride", fuente)

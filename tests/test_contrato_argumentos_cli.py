"""Ancla del contrato de argumentos CLI de los lanzadores de herramientas.

Por qué existe: cuatro runners salieron a producción pasando flags que las
herramientas **no declaran**, y ningún gate lo atajó. Verificado contra el código
fuente de cada herramienta:

- LOOT (`src/gui/qt/main.cpp`) declara ``--game``, ``--game-path``,
  ``--loot-data-path`` y ``--auto-sort``. El repo pasaba ``--sort``, que no
  existe: ``QCommandLineParser`` rechaza opciones desconocidas. Y lo pasaba desde
  **dos** lanzadores distintos (`local/loot/cli.py` y `app/core/windows_interop.py`),
  el patrón exacto de "dos superficies, un recurso" de `AGENTS.md`.
- Wrye Bash (`Mopy/bash/barg.py`) declara ``-b``/``--backup`` como *"Backup all
  Wrye Bash settings to an archive file"*. El repo lo usaba creyendo que
  construía el parche. **No existe ningún flag que construya el Bashed Patch.**
- BodySlide (`src/program/BodySlideApp.cpp::OnCmdLineParsed`) declara ``gbuild``,
  ``t``, ``p``, ``tri`` y ``preview``. El repo pasaba ``-b``/``-o``: ambos nombres
  inválidos.
- Pandora (README oficial, "Startup Arguments") declara ``--output``/``-o``,
  ``--auto_run``, ``--auto_close``, ``--tesv``. El repo agregaba ``--game`` y
  ``--auto``, que no existen.

Por qué los tests previos no lo vieron: **muestreaban**. `test_pandora_runner.py`
afirmaba ``argv.count("--output") == 1``, lo cual pasa igual con dos flags
inventados al lado. Ver "La regla que más se viola" en `AGENTS.md`: un caso
escrito a mano para el flag que te acordaste no ataja al que no.

Estos tests **enumeran**: congelan el inventario de módulos que lanzan
subprocesos y exigen que cada lanzador de herramienta de terceros tenga
procedencia declarada. Un lanzador nuevo —o uno existente que empiece a spawnear—
rompe el ancla hasta que se le documente contra qué fuente se verificaron sus
flags.
"""

from __future__ import annotations

import ast
import pathlib
from unittest.mock import AsyncMock, patch

import pytest

from sky_claw.local.tools.bodyslide_runner import BodySlideConfig, BodySlideRunner
from sky_claw.local.tools.pandora_runner import PandoraConfig, PandoraRunner
from sky_claw.local.tools.wrye_bash_runner import (
    WryeBashConfig,
    WryeBashHeadlessUnsupportedError,
    WryeBashRunner,
)

RAIZ = pathlib.Path(__file__).resolve().parents[1]
PAQUETE = RAIZ / "sky_claw"

#: Funciones que crean un proceso externo. `run_capture` es el helper propio
#: (`local/tools/_process.py`); el resto son las primitivas de asyncio/subprocess.
FUNCIONES_LANZADORAS = {
    "create_subprocess_exec",
    "create_subprocess_shell",
    "run_capture",
    "Popen",
}

#: Inventario congelado de módulos que lanzan procesos, con la cantidad exacta de
#: llamadas. Se congela el conteo y no solo el nombre: agregar un segundo spawn
#: dentro de un módulo ya listado es el punto de extensión más probable, y con un
#: set de nombres pasaría sin romper nada — que es exactamente cómo el segundo
#: lanzador de LOOT vivió en `windows_interop.py` sin que nadie lo notara.
LANZADORES_ESPERADOS = {
    # Infraestructura: lanzan procesos pero no cargan flags de herramientas de
    # terceros (helpers, brokers USVFS, wslpath, workers de prueba).
    "sky_claw/app/agent/executor.py": 1,
    "sky_claw/app/core/vfs_orchestrator.py": 1,
    "sky_claw/app/core/windows_interop.py": 2,
    "sky_claw/local/mo2/vfs_worker.py": 1,
    "sky_claw/local/tools/_process.py": 2,
    # Lanzadores de herramienta: cada uno debe tener entrada en PROCEDENCIA_DE_FLAGS.
    "sky_claw/local/loot/cli.py": 1,
    "sky_claw/local/loot/version.py": 1,
    "sky_claw/local/mo2/vfs.py": 1,
    "sky_claw/local/tools/bodyslide_runner.py": 1,
    "sky_claw/local/tools/dyndolod_runner.py": 1,
    "sky_claw/local/tools/pandora_runner.py": 1,
    "sky_claw/local/tools/synthesis_runner.py": 1,
    "sky_claw/local/tools/vramr_service.py": 1,
    "sky_claw/local/xedit/runner.py": 1,
}

#: Módulos que construyen un vector de argumentos para una herramienta de
#: terceros. Cada uno declara **contra qué fuente** se verificaron sus flags.
#:
#: La procedencia no es decorativa: los tres informes externos que motivaron esta
#: auditoría afirmaban flags distintos entre sí, todos con confianza y ninguno con
#: cita verificable. Lo único que resolvió las disputas fue leer el parser de cada
#: herramienta. Un runner sin procedencia es un runner que nadie verificó.
PROCEDENCIA_DE_FLAGS = {
    "sky_claw/local/loot/cli.py": "loot/loot — src/gui/qt/main.cpp (opciones Qt declaradas)",
    "sky_claw/local/loot/version.py": "loot/loot — src/gui/qt/main.cpp (addVersionOption)",
    "sky_claw/app/core/windows_interop.py": "loot/loot — src/gui/qt/main.cpp (lanzador legacy, deprecado)",
    "sky_claw/local/tools/bodyslide_runner.py": (
        "ousnius/BodySlide-and-Outfit-Studio — src/program/BodySlideApp.cpp::OnCmdLineParsed"
    ),
    "sky_claw/local/tools/pandora_runner.py": (
        "Monitor221hz/Pandora-Behaviour-Engine-Plus — README, sección 'Startup Arguments'"
    ),
    "sky_claw/local/tools/wrye_bash_runner.py": (
        "wrye-bash/wrye-bash — Mopy/bash/barg.py: no existe flag de build headless"
    ),
    # SIN VERIFICAR — pendientes de contrastar contra fuente. Se declaran
    # explícitamente para que la deuda sea visible en vez de silenciosa.
    "sky_claw/local/mo2/vfs.py": "SIN VERIFICAR — MO2 `-p` / `moshortcut://`",
    "sky_claw/local/tools/dyndolod_runner.py": "SIN VERIFICAR — DynDOLOD es binario cerrado, sin fuente pública",
    "sky_claw/local/tools/synthesis_runner.py": "SIN VERIFICAR — Mutagen-Modding/Synthesis, proyecto Synthesis.Bethesda.CLI",
    "sky_claw/local/tools/vramr_service.py": "SIN VERIFICAR — VRAMr se distribuye como scripts PowerShell en Nexus",
    "sky_claw/local/xedit/runner.py": "SIN VERIFICAR — TES5Edit/TES5Edit, Core/wbInit.pas",
}


def _es_llamada_lanzadora(func: ast.expr) -> bool:
    """``asyncio.create_subprocess_exec(...)`` o ``run_capture(...)`` importado directo."""
    if isinstance(func, ast.Attribute):
        return func.attr in FUNCIONES_LANZADORAS
    return isinstance(func, ast.Name) and func.id in FUNCIONES_LANZADORAS


def _contar_lanzamientos(arbol: ast.AST) -> int:
    return sum(1 for nodo in ast.walk(arbol) if isinstance(nodo, ast.Call) and _es_llamada_lanzadora(nodo.func))


def _lanzadores_por_modulo() -> dict[str, int]:
    encontrados: dict[str, int] = {}
    for archivo in PAQUETE.rglob("*.py"):
        arbol = ast.parse(archivo.read_text(encoding="utf-8"), filename=str(archivo))
        cantidad = _contar_lanzamientos(arbol)
        if cantidad:
            encontrados[archivo.relative_to(RAIZ).as_posix()] = cantidad
    return encontrados


def _contar(fuente: str) -> int:
    return _contar_lanzamientos(ast.parse(fuente))


# --------------------------------------------------------------------------- #
# El detector
# --------------------------------------------------------------------------- #


def test_detector_reconoce_las_formas_de_lanzar_un_proceso() -> None:
    """El ancla no sirve si se la esquiva cambiando la forma de invocar."""
    assert _contar("import asyncio\nasyncio.create_subprocess_exec('a')\n") == 1
    assert _contar("import asyncio\nasyncio.create_subprocess_shell('a')\n") == 1
    assert _contar("from x import run_capture\nrun_capture(['a'])\n") == 1
    assert _contar("import subprocess\nsubprocess.Popen(['a'])\n") == 1


def test_detector_ignora_menciones_que_no_son_llamadas() -> None:
    """Docstrings y referencias sin llamar no cuentan."""
    assert _contar('"""No usar create_subprocess_exec() directo."""\n') == 0
    assert _contar("# run_capture(args) queda prohibido\nx = 1\n") == 0
    assert _contar("from x import run_capture\nfn = run_capture\n") == 0


def test_detector_cuenta_cada_llamada_no_cada_modulo() -> None:
    """Congelar solo el nombre del módulo dejaba libre agregar un segundo spawn."""
    fuente = "from x import run_capture\nrun_capture(['a'])\nrun_capture(['b'])\n"
    assert _contar(fuente) == 2


# --------------------------------------------------------------------------- #
# El inventario congelado
# --------------------------------------------------------------------------- #


def test_lanzadores_de_subproceso_estan_congelados() -> None:
    """Igualdad literal del dict: módulo nuevo *o* spawn nuevo rompe acá."""
    assert _lanzadores_por_modulo() == LANZADORES_ESPERADOS, (
        "Cambió el inventario de módulos que lanzan procesos externos. Si es un "
        "lanzador de herramienta nuevo, agregalo también a PROCEDENCIA_DE_FLAGS "
        "declarando contra qué fuente verificaste sus argumentos — no contra un "
        "informe ni contra lo que hacía el runner de al lado."
    )


def test_todo_lanzador_de_herramienta_declara_procedencia() -> None:
    """Un runner sin procedencia es un runner que nadie verificó."""
    faltantes = sorted(m for m in PROCEDENCIA_DE_FLAGS if not PROCEDENCIA_DE_FLAGS[m].strip())
    assert not faltantes, f"Procedencia vacía en {faltantes}"


def test_la_procedencia_apunta_a_modulos_que_existen() -> None:
    """Evita que la tabla envejezca apuntando a archivos renombrados o borrados."""
    inexistentes = sorted(m for m in PROCEDENCIA_DE_FLAGS if not (RAIZ / m).is_file())
    assert not inexistentes, f"PROCEDENCIA_DE_FLAGS apunta a módulos inexistentes: {inexistentes}"


# --------------------------------------------------------------------------- #
# Los vectores de argumentos, contra fuente
# --------------------------------------------------------------------------- #


def test_ningun_lanzador_de_loot_usa_el_flag_inexistente() -> None:
    """``--sort`` no está declarado en el parser de LOOT; el flag real es ``--auto-sort``.

    Se chequea sobre el árbol entero y no sobre un runner puntual: el defecto
    original vivía en DOS módulos, y arreglar uno solo es el modo de fallo
    dominante de este repo.
    """
    ofensores = sorted(
        archivo.relative_to(RAIZ).as_posix()
        for archivo in PAQUETE.rglob("*.py")
        if '"--sort"' in archivo.read_text(encoding="utf-8") or "'--sort'" in archivo.read_text(encoding="utf-8")
    )
    assert not ofensores, (
        f"`--sort` reaparece en {ofensores}. LOOT declara `--auto-sort` "
        "(src/gui/qt/main.cpp); `--sort` hace que QCommandLineParser rechace la invocación."
    )


async def test_loot_construye_el_vector_verificado(tmp_path: pathlib.Path) -> None:
    """Vector completo contra el parser de LOOT, no un flag muestreado."""
    from sky_claw.local.loot.cli import LOOTConfig, LOOTRunner

    loot_exe = tmp_path / "loot.exe"
    loot_exe.touch()
    juego = tmp_path / "Skyrim Special Edition"
    juego.mkdir()
    runner = LOOTRunner(LOOTConfig(loot_exe=loot_exe, game_path=juego, game="Skyrim Special Edition"))

    capturado: dict[str, list[str]] = {}

    async def fake_exec(*args: str, **_kwargs: object) -> AsyncMock:
        capturado["args"] = list(args)
        proc = AsyncMock()
        proc.communicate = AsyncMock(return_value=(b"", b""))
        proc.returncode = 0
        return proc

    with (
        patch("sky_claw.local.loot.cli.asyncio.create_subprocess_exec", fake_exec),
        patch("sky_claw.local.loot.cli.translate_path_if_wsl", return_value=str(juego)),
    ):
        await runner.sort()

    argv = capturado["args"]
    assert "--auto-sort" in argv, "LOOT ordena con --auto-sort"
    assert "--sort" not in argv
    assert argv[argv.index("--game") + 1] == "Skyrim Special Edition"
    assert "--game-path" in argv


async def test_bodyslide_construye_el_vector_verificado(tmp_path: pathlib.Path) -> None:
    """BodySlide declara ``gbuild`` y ``t``; ``-b``/``-o`` no son opciones válidas."""
    runner = BodySlideRunner(BodySlideConfig(bodyslide_exe=tmp_path / "BodySlide.exe", game_path=tmp_path))
    run_capture = AsyncMock(return_value=(b"ok", b"", 0))

    with patch("sky_claw.local.tools.bodyslide_runner.run_capture", run_capture):
        await runner.run_batch("CBBE Body", str(tmp_path / "salida"))

    argv = run_capture.await_args.args[0]
    assert argv[1:] == ["-gbuild", "CBBE Body", "-t", str(tmp_path / "salida")]
    assert "-b" not in argv, "`-b` no es una opción declarada por BodySlide"
    assert "-o" not in argv, "`-o` no es una opción declarada por BodySlide"


async def test_pandora_no_pasa_flags_inexistentes(tmp_path: pathlib.Path) -> None:
    """``--game`` y ``--auto`` no existen en Pandora; el output sí es ``--output``."""
    juego = tmp_path / "Skyrim"
    runner = PandoraRunner(PandoraConfig(pandora_exe=tmp_path / "Pandora.exe", game_path=juego))
    run_capture = AsyncMock(return_value=(b"ok", b"", 0))

    with patch("sky_claw.local.tools.pandora_runner.run_capture", run_capture):
        await runner.run_pandora()

    argv = run_capture.await_args.args[0]
    assert "--game" not in argv, "Pandora no declara `--game` (la ruta del juego es `--tesv`)"
    assert "--auto" not in argv, "Pandora declara `--auto_run`/`--auto_close`, no `--auto`"
    assert argv.count("--output") == 1
    assert pathlib.Path(argv[argv.index("--output") + 1]) == juego.resolve() / "Pandora_Output"


async def test_wrye_bash_falla_cerrado_en_vez_de_hacer_un_backup(tmp_path: pathlib.Path) -> None:
    """Wrye Bash no expone build headless: antes corría ``-b`` (=``--backup``) y decía éxito.

    Reportar ``success`` sobre un backup de settings es peor que fallar: las
    etapas siguientes del DAG (Synthesis, DynDOLOD) consumen un parche que nunca
    se regeneró.
    """
    runner = WryeBashRunner(
        WryeBashConfig(
            wrye_bash_path=tmp_path / "bash.py",
            game_path=tmp_path,
            mo2_path=tmp_path,
        )
    )
    with pytest.raises(WryeBashHeadlessUnsupportedError, match="headless"):
        await runner.generate_bashed_patch()

    # Que además no lance ningún proceso lo garantiza `LANZADORES_ESPERADOS`:
    # `wrye_bash_runner.py` ya no figura en el inventario de módulos que spawnean.
    assert "sky_claw/local/tools/wrye_bash_runner.py" not in _lanzadores_por_modulo()

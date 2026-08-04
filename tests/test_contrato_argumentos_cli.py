"""Ancla del contrato de argumentos CLI de los lanzadores de herramientas.

Por qué existe: cuatro runners salieron a producción pasando flags que las
herramientas **no declaran**, y ningún gate lo atajó. Verificado contra el código
fuente de cada herramienta:

- LOOT (`src/gui/qt/main.cpp`) declara ``--game``, ``--game-path``,
  ``--loot-data-path`` y ``--auto-sort``. El repo pasaba ``--sort``, que no
  existe: ``QCommandLineParser`` rechaza opciones desconocidas. Y lo pasaba desde
  **dos** lanzadores distintos (`local/loot/cli.py` y `app/core/windows_interop.py`),
  el patrón exacto de "dos superficies, un recurso" de `AGENTS.md`.
- Wrye Bash (`Mopy/bash/barg.py`) declara ``-b``/``--backup`` como
  ``action='store_true'`` (*"Backup all Wrye Bash settings to an archive file"*,
  con el destino aparte en ``-f``/``--filename``). El repo lo usaba creyendo que
  construía el parche, y le pasaba el nombre del .esp como valor: ``-b`` no toma
  valor y el parser no declara posicionales, así que ``parse_args()`` abortaba.
  **No existe ningún flag que construya el Bashed Patch.**
- BodySlide (`src/program/BodySlideApp.cpp::OnCmdLineParsed`, con el descriptor
  en `BodySlideApp.h::g_cmdLineDesc`) declara ``gbuild``, ``t``, ``p``, ``tri`` y
  ``preview`` como nombres CORTOS de `wxCmdLineParser` — de ahí el guion simple
  en ``-gbuild``/``-t``. El repo pasaba ``-b``/``-o``: ambos nombres inválidos.
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

Segunda ronda (revisión automática de PR #426), el mismo defecto en el propio
ancla y en el propio fix:

- El detector NO contaba ``subprocess.run``/``.call``/``.check_call``/
  ``.check_output`` ni ``os.system``/``.popen`` — 9 spawns reales en
  `windows_interop.py`, `_process.py` y `file_permissions.py` quedaban fuera del
  inventario que este archivo dice congelar.
- `test_todo_lanzador_de_herramienta_declara_procedencia` solo validaba que las
  entradas YA EXISTENTES de `PROCEDENCIA_DE_FLAGS` no estuvieran vacías — un
  lanzador nuevo podía sumarse a `LANZADORES_ESPERADOS` sin declarar procedencia
  y el test seguía en verde.
- El propio fix de LOOT dejó un segundo flag inventado sin corregir:
  ``--update-masterlist`` tampoco es un flag real de LOOT (mismo
  `src/gui/qt/main.cpp`) — arreglar `--sort` sin verificar el flag de al lado
  fue, otra vez, el defecto de "arreglar un hermano y no al otro".
- Pandora quedó sin ningún flag de automatización tras quitar los inventados:
  peor que antes, porque el proceso queda esperando en GUI hasta el timeout.
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

#: Funciones que crean un proceso externo, sin necesitar resolver el alias del
#: módulo que las expone. `run_capture` es el helper propio (`local/tools/_process.py`);
#: el resto son las primitivas de asyncio/subprocess cuyo nombre de atributo no
#: coincide con ningún método de dominio del repo (a diferencia de `run`, ver abajo).
FUNCIONES_LANZADORAS = {
    "create_subprocess_exec",
    "create_subprocess_shell",
    "run_capture",
    "Popen",
}

#: `subprocess.run`/`.call`/`.check_call`/`.check_output` y `os.system`/`.popen`
#: SÍ necesitan resolver el alias del módulo importado: `.run(...)` a secas
#: matchea de más — el repo tiene decenas de `preflight.run()`, `daemon.run()`,
#: `flow.run()` que no lanzan ningún proceso. Sin este alias-tracking, agregar
#: "run" a `FUNCIONES_LANZADORAS` infla el inventario con falsos positivos y
#: rompe el propósito del ancla (señalado en revisión: PR #426).
ATRIBUTOS_DE_SUBPROCESS = {"run", "call", "check_call", "check_output"}
ATRIBUTOS_DE_OS = {"system", "popen"}


def _alias_de_modulo(arbol: ast.AST, modulo: str) -> set[str]:
    """Nombres bajo los que `arbol` importa `modulo` entero (`import X as Y` → `{Y}`).

    Solo cubre ``import X``/``import X as Y``. ``from X import atributo`` se
    resuelve aparte en :func:`_nombres_directos_de`, porque ahí el nombre local
    apunta a la FUNCIÓN, no al módulo — mezclarlos en un solo set produciría
    falsos negativos (`from subprocess import run` nunca matchea `base in alias`
    porque no hay `base`, es una llamada a secas) o falsos positivos si se
    tratara como alias de módulo.
    """
    alias: set[str] = set()
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.Import):
            for nombre in nodo.names:
                if nombre.name == modulo:
                    alias.add(nombre.asname or modulo)
    return alias


def _nombres_directos_de(arbol: ast.AST, modulo: str, atributos: set[str]) -> set[str]:
    """Nombres locales para ``from modulo import atributo [as alias]``.

    Sin esto, ``from subprocess import run`` seguido de ``run(['a'])`` evade el
    detector por completo: `_alias_de_modulo` solo mira `ast.Import`, nunca
    `ast.ImportFrom` (hallazgo de revisión automática, PR #426) — el spawn más
    fácil de introducir por accidente quedaba fuera del inventario que este
    archivo dice congelar.
    """
    nombres: set[str] = set()
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.ImportFrom) and nodo.module == modulo:
            for alias in nodo.names:
                if alias.name in atributos:
                    nombres.add(alias.asname or alias.name)
    return nombres


#: Inventario congelado de módulos que lanzan procesos, con la cantidad exacta de
#: llamadas. Se congela el conteo y no solo el nombre: agregar un segundo spawn
#: dentro de un módulo ya listado es el punto de extensión más probable, y con un
#: set de nombres pasaría sin romper nada — que es exactamente cómo el segundo
#: lanzador de LOOT vivió en `windows_interop.py` sin que nadie lo notara.
LANZADORES_ESPERADOS = {
    # Infraestructura: lanzan procesos pero no cargan flags de herramientas de
    # terceros (helpers, brokers USVFS, wslpath, workers de prueba, utilidades
    # de sistema como taskkill/icacls/whoami/powershell).
    "sky_claw/app/agent/executor.py": 1,
    "sky_claw/app/core/vfs_orchestrator.py": 1,
    "sky_claw/app/security/file_permissions.py": 7,
    "sky_claw/local/mo2/vfs_worker.py": 1,
    "sky_claw/local/tools/_process.py": 3,
    # `subprocess.run(["7z", ...])` para listar/extraer archives descargados
    # (xEdit/Pandora) con sandbox de zip-slip — utilidad de archivado, no el
    # CLI propio de ninguna herramienta de modding. Aterrizó en `main` (#418)
    # mientras este PR estaba abierto; detectado por el ancla al mergear, que
    # es exactamente el trabajo para el que existe.
    "sky_claw/local/tools_installer.py": 2,
    # Mixto: además del `create_subprocess_exec` legacy que SÍ es lanzador de
    # LOOT (con entrada propia en PROCEDENCIA_DE_FLAGS), este módulo tiene un
    # `subprocess.run(["taskkill", ...])` puramente infra — mismo módulo, dos
    # naturalezas distintas, un solo conteo agregado.
    "sky_claw/app/core/windows_interop.py": 3,
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

#: Subconjunto de `LANZADORES_ESPERADOS` que lanza una herramienta de terceros
#: (no infraestructura pura) y por lo tanto DEBE tener entrada en
#: `PROCEDENCIA_DE_FLAGS`. Separado del dict de arriba porque la pertenencia a
#: este set es una decisión de clasificación, no algo derivable del conteo de
#: llamadas — un módulo puede spawnear una sola vez y aun así ser infra
#: (`vfs_worker.py`) o spawnear para un tercero (`pandora_runner.py`).
LANZADORES_DE_HERRAMIENTA = frozenset(
    {
        "sky_claw/local/loot/cli.py",
        "sky_claw/local/loot/version.py",
        "sky_claw/app/core/windows_interop.py",
        "sky_claw/local/mo2/vfs.py",
        "sky_claw/local/tools/bodyslide_runner.py",
        "sky_claw/local/tools/dyndolod_runner.py",
        "sky_claw/local/tools/pandora_runner.py",
        "sky_claw/local/tools/synthesis_runner.py",
        "sky_claw/local/tools/vramr_service.py",
        "sky_claw/local/xedit/runner.py",
        "sky_claw/local/tools/wrye_bash_runner.py",
    }
)

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


def _es_llamada_lanzadora(
    func: ast.expr,
    alias_subprocess: set[str],
    alias_os: set[str],
    nombres_directos: set[str],
) -> bool:
    """``asyncio.create_subprocess_exec(...)``, ``subprocess.run(...)``,
    ``from subprocess import run; run(...)``, etc."""
    if isinstance(func, ast.Attribute):
        if func.attr in FUNCIONES_LANZADORAS:
            return True
        base = func.value.id if isinstance(func.value, ast.Name) else None
        if func.attr in ATRIBUTOS_DE_SUBPROCESS and base in alias_subprocess:
            return True
        return func.attr in ATRIBUTOS_DE_OS and base in alias_os
    return isinstance(func, ast.Name) and (func.id in FUNCIONES_LANZADORAS or func.id in nombres_directos)


def _contar_lanzamientos(arbol: ast.AST) -> int:
    alias_subprocess = _alias_de_modulo(arbol, "subprocess")
    alias_os = _alias_de_modulo(arbol, "os")
    nombres_directos = _nombres_directos_de(arbol, "subprocess", ATRIBUTOS_DE_SUBPROCESS) | _nombres_directos_de(
        arbol, "os", ATRIBUTOS_DE_OS
    )
    return sum(
        1
        for nodo in ast.walk(arbol)
        if isinstance(nodo, ast.Call) and _es_llamada_lanzadora(nodo.func, alias_subprocess, alias_os, nombres_directos)
    )


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
    assert _contar("import subprocess\nsubprocess.run(['a'])\n") == 1
    assert _contar("import subprocess\nsubprocess.call(['a'])\n") == 1
    assert _contar("import subprocess\nsubprocess.check_call(['a'])\n") == 1
    assert _contar("import subprocess\nsubprocess.check_output(['a'])\n") == 1
    assert _contar("import os\nos.system('a')\n") == 1
    assert _contar("import os\nos.popen('a')\n") == 1
    # Alias de import: `subprocess.run` sigue siendo `subprocess.run` con otro nombre.
    assert _contar("import subprocess as sp\nsp.run(['a'])\n") == 1


def test_detector_reconoce_from_import_de_subprocess_y_os() -> None:
    """``from subprocess import run`` seguido de ``run(...)`` a secas es un spawn.

    `_alias_de_modulo` solo caza ``import subprocess``; sin resolver también
    ``ast.ImportFrom`` (hallazgo de revisión automática, PR #426), este es el
    spawn más fácil de introducir por accidente — y el que efectivamente vive
    en `tools_installer.py` en su forma `import subprocess` LOCAL dentro de una
    función, que sigue cubierta porque `ast.walk` no distingue scope; el caso
    que faltaba era la forma `from`.
    """
    assert _contar("from subprocess import run\nrun(['a'])\n") == 1
    assert _contar("from subprocess import call\ncall(['a'])\n") == 1
    assert _contar("from subprocess import check_call\ncheck_call(['a'])\n") == 1
    assert _contar("from subprocess import check_output\ncheck_output(['a'])\n") == 1
    assert _contar("from os import system\nsystem('a')\n") == 1
    assert _contar("from os import popen\npopen('a')\n") == 1
    # Alias: `from subprocess import run as ejecutar` sigue siendo un spawn.
    assert _contar("from subprocess import run as ejecutar\nejecutar(['a'])\n") == 1
    # Un `from` de un módulo ajeno con un atributo homónimo no cuenta.
    assert _contar("from otro_modulo import run\nrun(['a'])\n") == 0


def test_detector_no_confunde_metodo_run_de_dominio_con_spawn() -> None:
    """`.run()` a secas NO es una señal de spawn: el repo tiene decenas de
    `preflight.run()` / `daemon.run()` / `flow.run()` que no lanzan ningún
    proceso. Agregar "run" a `FUNCIONES_LANZADORAS` sin resolver el alias del
    módulo (el error que motivó este test, señalado en revisión) infla el
    inventario con falsos positivos.
    """
    assert _contar("preflight = _Preflight()\npreflight.run()\n") == 0
    assert _contar("await self._maintenance_daemon.run()\n") == 0
    # Ni siquiera si el objeto se llama literalmente `subprocess` sin haberlo
    # importado como tal — el alias se resuelve por el `import`, no por el nombre.
    assert _contar("subprocess = MiWrapper()\nsubprocess.run(['a'])\n") == 0


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


def test_todo_lanzador_de_herramienta_tiene_entrada_en_procedencia() -> None:
    """Un lanzador de herramienta nuevo debe romper este test hasta declarar procedencia.

    El test anterior solo valida que las entradas EXISTENTES de
    `PROCEDENCIA_DE_FLAGS` no estén vacías — un módulo agregado a
    `LANZADORES_DE_HERRAMIENTA` sin agregar también su entrada en
    `PROCEDENCIA_DE_FLAGS` pasaría sin romper nada, exactamente el hueco que el
    ancla dice cerrar (señalado en revisión: PR #426).
    """
    faltantes = sorted(LANZADORES_DE_HERRAMIENTA - set(PROCEDENCIA_DE_FLAGS))
    assert not faltantes, f"Lanzadores de herramienta sin PROCEDENCIA_DE_FLAGS: {faltantes}"


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


def test_ningun_lanzador_de_loot_usa_el_flag_de_masterlist_inexistente() -> None:
    """``--update-masterlist`` tampoco está declarado en el parser de LOOT.

    El hermano del defecto de arriba, encontrado por revisión automática dentro
    del propio fix: corregir `--sort` sin verificar el flag de al lado dejó
    `--update-masterlist` pasando el filtro entero — que rompe la invocación
    exactamente igual que `--sort` la rompía.
    """
    ofensores = sorted(
        archivo.relative_to(RAIZ).as_posix()
        for archivo in PAQUETE.rglob("*.py")
        if '"--update-masterlist"' in archivo.read_text(encoding="utf-8")
        or "'--update-masterlist'" in archivo.read_text(encoding="utf-8")
    )
    assert not ofensores, (
        f"`--update-masterlist` reaparece en {ofensores}. No es un flag declarado por LOOT "
        "(src/gui/qt/main.cpp); refrescar el masterlist requiere MasterlistDownloader, no un flag CLI."
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


async def test_loot_con_update_masterlist_no_agrega_el_flag_inexistente(tmp_path: pathlib.Path) -> None:
    """``update_masterlist=True`` (default de producción) no debe romper la invocación.

    Antes de este fix, `sort(update_masterlist=True)` —el camino que usa
    `loot_service.py` por defecto en producción— agregaba `--update-masterlist`,
    que `QCommandLineParser` rechaza junto con TODO el resto de argumentos
    válidos. Ejercitar solo la rama `False` (como hacía la versión anterior de
    este test) dejaba pasar exactamente el caso que más importa: el real.
    """
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
        await runner.sort(update_masterlist=True)

    assert capturado["args"] == [
        str(loot_exe),
        "--game",
        "Skyrim Special Edition",
        "--game-path",
        str(juego),
        "--auto-sort",
    ], "--update-masterlist no es un flag real de LOOT; se afirma el vector completo, no un membership"


async def test_bodyslide_construye_el_vector_verificado(tmp_path: pathlib.Path) -> None:
    """BodySlide declara ``gbuild``, ``t``, ``p`` y ``tri``; ``-b``/``-o`` no son válidas.

    ``-tri`` va por defecto: sin él, ``GroupBuild`` pasa ``cmdTri=false`` a
    ``BuildListBodies`` y no se emiten los ``.tri``, que son los que OBody NG /
    RaceMenu cargan en memoria para los morfos dinámicos. Compilar sin morfos es
    un build silenciosamente incompleto, no una variante válida por defecto.
    """
    runner = BodySlideRunner(BodySlideConfig(bodyslide_exe=tmp_path / "BodySlide.exe", game_path=tmp_path))
    run_capture = AsyncMock(return_value=(b"ok", b"", 0))
    salida = tmp_path / "salida"
    salida.mkdir()
    (salida / "cuerpo.nif").write_bytes(b"\x00")

    with patch("sky_claw.local.tools.bodyslide_runner.run_capture", run_capture):
        await runner.run_batch("CBBE Body", str(salida), preset="Zeroed Sliders")

    argv = run_capture.await_args.args[0]
    assert argv[1:] == ["-gbuild", "CBBE Body", "-t", str(salida), "-p", "Zeroed Sliders", "-tri"]
    assert "-b" not in argv, "`-b` no es una opción declarada por BodySlide"
    assert "-o" not in argv, "`-o` no es una opción declarada por BodySlide"


async def test_bodyslide_omite_preset_y_tri_cuando_no_se_piden(tmp_path: pathlib.Path) -> None:
    """``-p`` se omite sin preset (no se emite vacío) y ``-tri`` es opt-out real.

    Emitir ``-p ""`` haría que BodySlide sobrescriba ``SelectedPreset`` con vacío;
    omitirlo conserva el comportamiento declarado ("defaults to last used preset").
    """
    runner = BodySlideRunner(BodySlideConfig(bodyslide_exe=tmp_path / "BodySlide.exe", game_path=tmp_path))
    run_capture = AsyncMock(return_value=(b"ok", b"", 0))
    salida = tmp_path / "salida"
    salida.mkdir()
    (salida / "cuerpo.nif").write_bytes(b"\x00")

    with patch("sky_claw.local.tools.bodyslide_runner.run_capture", run_capture):
        await runner.run_batch("CBBE Body", str(salida), build_morphs=False)

    argv = run_capture.await_args.args[0]
    assert argv[1:] == ["-gbuild", "CBBE Body", "-t", str(salida)]


async def test_pandora_construye_el_vector_verificado(tmp_path: pathlib.Path) -> None:
    """Vector completo contra el README de Pandora, no flags muestreados.

    ``count("--output") == 1`` pasaba igual con ``--game``/``--auto`` inventados
    al lado (señalado en revisión: PR #426) — exactamente la clase de test que
    este ancla existe para reemplazar. Se afirma igualdad de lista completa.
    """
    juego = tmp_path / "Skyrim"
    runner = PandoraRunner(PandoraConfig(pandora_exe=tmp_path / "Pandora.exe", game_path=juego))
    run_capture = AsyncMock(return_value=(b"ok", b"", 0))

    with patch("sky_claw.local.tools.pandora_runner.run_capture", run_capture):
        await runner.run_pandora()

    argv = run_capture.await_args.args[0]
    assert argv[1:] == [
        "--auto_run",
        "--auto_close",
        "--tesv",
        str(juego.resolve()),
        "--output",
        str(juego.resolve() / "Pandora_Output"),
    ], "Pandora no declara `--game` ni `--auto` (README: `--tesv`, `--auto_run`/`--auto_close`)"


async def test_wrye_bash_falla_cerrado_en_vez_de_lanzar_un_comando_invalido(tmp_path: pathlib.Path) -> None:
    """Wrye Bash no expone build headless: antes corría ``-b`` (=``--backup``, sin valor).

    El comando anterior ni siquiera parseaba —``argparse`` cortaba por el .esp
    posicional y por el ``-f`` faltante—, así que la etapa 6 fallaba con un exit
    code sin causa legible. Elevar la excepción nombrada es lo que le dice al
    operador que la etapa requiere GUI; y cierra el riesgo simétrico de que una
    variante que SÍ parsee haga un backup de settings, salga con código 0 y
    reporte ``success`` sobre un parche que las etapas 7 y 9 del DAG consumen sin
    haberse regenerado.
    """
    runner = WryeBashRunner(
        WryeBashConfig(
            wrye_bash_path=tmp_path / "bash.py",
            game_path=tmp_path,
            mo2_path=tmp_path,
        )
    )
    # Guardia estática (inventario AST) + guardia en runtime (el punto de
    # entrada real de creación de procesos, interceptado): la primera protege
    # contra un import nuevo de `run_capture`/`subprocess`; la segunda contra
    # que algún camino oculto —p. ej. un wrapper que no pasa por esos nombres—
    # spawnee sin que el AST lo vea (señalado en revisión: PR #426).
    with (
        patch("asyncio.create_subprocess_exec") as spawn_mock,
        pytest.raises(WryeBashHeadlessUnsupportedError, match="headless"),
    ):
        await runner.generate_bashed_patch()

    spawn_mock.assert_not_called()
    assert "sky_claw/local/tools/wrye_bash_runner.py" not in _lanzadores_por_modulo()

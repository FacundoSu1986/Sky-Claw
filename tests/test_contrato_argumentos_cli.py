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
- DynDOLOD/TexGen (``dyndolod.info/Help/Command-Line-Argument``, doc del paquete
  ``Docs/DynDOLOD-Shortcut.txt`` y strings del binario — PE Subsystem 2, GUI): el
  repo pasaba ``-game SSE``, ``-p <preset>``, ``-t`` pelado y ``--expert``. El
  parser que ambos binarios heredan de xEdit (``xeInit.pas``) solo conoce el game
  mode como switch suelto (``-sse``/``-tes5vr``) y los ``-X:"ruta"`` con dos
  puntos; ``--expert`` no es un argumento (se activa con ``Expert=1`` en el INI)
  y el preset se elige en el asistente GUI — la etapa 9 es asistida. Verificado
  por efecto en rig 2026-08-05 (``Game Mode: SSE``, ``Using Temp Path:``). xEdit
  (``xeInit.pas``: ``-T:``/``-P:``) emitía el vector correcto
  (``-SSE``/``-autoload``/``-autoexit``, anclado en ``test_xedit_quick_clean.py``);
  solo faltaba la procedencia declarada, que se agrega acá.

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
import subprocess
import sys
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from sky_claw.local.tools.bodyslide_runner import BodySlideConfig, BodySlideRunner

# Import a nivel de módulo (el resto del archivo importa dyndolod dentro de cada
# test) porque `@parametrize` se evalúa en tiempo de colección: es lo que hace que
# la enumeración de los switches administrados SALGA del código en vez de ser una
# tercera lista escrita a mano que puede desalinearse en silencio.
from sky_claw.local.tools.dyndolod_runner import _LETRAS_ADMINISTRADAS
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
    "sky_claw/local/tools/dyndolod_runner.py": (
        "DynDOLOD — dyndolod.info/Help/Command-Line-Argument + Docs/DynDOLOD-Shortcut.txt "
        "del paquete + strings del binario (Subsystem=2, GUI)"
    ),
    "sky_claw/local/tools/synthesis_runner.py": "SIN VERIFICAR — Mutagen-Modding/Synthesis, proyecto Synthesis.Bethesda.CLI",
    "sky_claw/local/tools/vramr_service.py": "SIN VERIFICAR — VRAMr se distribuye como scripts PowerShell en Nexus",
    "sky_claw/local/xedit/runner.py": "TES5Edit/TES5Edit — xEdit/xeInit.pas (-T:/-P:, game mode)",
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


def test_ningun_lanzador_usa_el_flag_inexistente_de_dyndolod() -> None:
    """``-game`` no existe en el parser de xEdit/DynDOLOD: el game mode es el
    switch suelto ``-sse``/``-tes5vr`` (xeInit.pas).

    Token EXACTO (``"-game"`` con comilla final): ``--game`` de LOOT contiene
    ``-game`` como substring, así que buscar el substring daría un falso positivo
    en el módulo de LOOT — el mismo flag, dos superficies, una legítima.
    """
    ofensores = sorted(
        archivo.relative_to(RAIZ).as_posix()
        for archivo in PAQUETE.rglob("*.py")
        if '"-game"' in (texto := archivo.read_text(encoding="utf-8")) or "'-game'" in texto
    )
    assert not ofensores, (
        f"`-game` reaparece en {ofensores}. El game mode de xEdit/DynDOLOD es el switch "
        "suelto `-sse`/`-tes5vr` (xeInit.pas); `-game` se ignora en silencio y SSE queda "
        "como posicional suelto."
    )


def test_ningun_lanzador_usa_el_expert_inexistente() -> None:
    """``--expert`` no es un argumento de DynDOLOD: se activa con ``Expert=1`` en
    el INI. El vector viejo lo pasaba "para prevenir bloqueos UI" — inventado."""
    ofensores = sorted(
        archivo.relative_to(RAIZ).as_posix()
        for archivo in PAQUETE.rglob("*.py")
        if '"--expert"' in (texto := archivo.read_text(encoding="utf-8")) or "'--expert'" in texto
    )
    assert not ofensores, (
        f"`--expert` reaparece en {ofensores}. No es un argumento declarado por DynDOLOD: "
        "se activa con Expert=1 en DynDOLOD_SSE.ini."
    )


def test_switches_de_ruta_de_dyndolod_siempre_llevan_dos_puntos() -> None:
    """``-t``/``-p`` pelados (sin ``:``) se ignoran en silencio en el parser de
    xEdit (``xeInit.pas``) — así vivió el defecto sin dar señal.

    El alcance lo da el GLOB (``*dyndolod*.py``), no una tupla manual de nombres:
    un módulo futuro ``dyndolod_*.py`` (un split del runner, un CLI nuevo) entra
    al ancla automáticamente — regla "enumerar, no muestrear". BodySlide declara
    ``-t``/``-p`` como nombres cortos de ``wxCmdLineParser`` y MO2 usa ``-p``
    (perfil): tokens legítimos que el glob nunca matchea, por eso la exclusión
    no necesita enumerarlos.
    """
    ofensores = sorted(
        archivo.relative_to(RAIZ).as_posix()
        for archivo in PAQUETE.rglob("*dyndolod*.py")
        if (
            '"-t"' in (texto := archivo.read_text(encoding="utf-8"))
            or "'-t'" in texto
            or '"-p"' in texto
            or "'-p'" in texto
        )
    )
    assert not ofensores, (
        f"`-t`/`-p` pelados reaparecen en {ofensores}. El contrato exige `-t:<ruta>`/`-p:<ruta>` "
        "con dos puntos: sin `:` el switch se ignora en silencio. (Sin comillas: las pone "
        "`list2cmdline` al serializar — ver `_switch_de_ruta`.)"
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


async def test_dyndolod_construye_el_vector_verificado(tmp_path: pathlib.Path) -> None:
    """Vector completo contra la doc oficial de DynDOLOD, no flags muestreados.

    El vector viejo (``-game SSE``, ``-p <preset>``, ``-t`` pelado, ``--expert``)
    no existe en la herramienta: el parser de xEdit (``xeInit.pas``) solo conoce
    el game mode como switch suelto (``-sse``) y los ``-X:<ruta>`` con dos puntos.
    ``--expert`` no es un argumento (se activa con ``Expert=1`` en el INI) y el
    preset lo elige el humano en el asistente — por eso NO puede aparecer en el
    argv bajo ninguna config. TexGen y DynDOLOD comparten el parser: mismo vector.

    Los elementos van **sin comillas**: las comillas de la doc son de la línea de
    comandos y las agrega ``list2cmdline`` al serializar. Este test afirma el
    string que Sky-Claw produce; que ese string llegue intacto al proceso lo
    afirma ``test_dyndolod_argv_sobrevive_la_serializacion_de_windows`` — la
    distinción es el defecto que el rig encontró el 2026-08-10.
    """
    from sky_claw.local.tools.dyndolod_runner import DynDOLODConfig, DynDOLODRunner

    game = tmp_path / "Skyrim Special Edition"
    game.mkdir()
    exe_dir = tmp_path / "DynDOLOD"
    exe_dir.mkdir()
    dyndolod_exe = exe_dir / "DynDOLODx64.exe"
    dyndolod_exe.touch()
    texgen_exe = exe_dir / "TexGenx64.exe"
    texgen_exe.touch()
    ini_dir = tmp_path / "My Games" / "Skyrim Special Edition"
    ini_dir.mkdir(parents=True)
    plugins_file = tmp_path / "Plugins.txt"
    plugins_file.touch()
    output_root = tmp_path / "salida"
    temp_dir = tmp_path / "temp"

    config = DynDOLODConfig(
        game_path=game,
        mo2_path=tmp_path / "MO2",
        mo2_mods_path=tmp_path / "MO2" / "mods",
        dyndolod_exe=dyndolod_exe,
        texgen_exe=texgen_exe,
        data_dir=game / "Data",
        ini_dir=ini_dir,
        plugins_file=plugins_file,
        output_root=output_root,
        temp_dir=temp_dir,
    )
    runner = DynDOLODRunner(config)

    esperado = [
        "-sse",
        f"-o:{output_root}\\",
        f"-d:{game / 'Data'}\\",
        f"-m:{ini_dir}\\",
        f"-p:{plugins_file}",
        f"-t:{temp_dir}\\",
    ]

    with patch.object(runner, "_execute_process", AsyncMock(return_value=("", "", 0, 1.0))) as ejecutar:
        await runner.run_texgen()
        await runner.run_dyndolod(preset="Medium")
        assert ejecutar.call_count == 2
        texgen_argv = ejecutar.call_args_list[0].kwargs["args"]
        dyndolod_argv = ejecutar.call_args_list[1].kwargs["args"]

    assert texgen_argv == esperado, "TexGen debe emitir exactamente el vector verificado"
    assert dyndolod_argv == esperado, "DynDOLOD comparte el parser de xEdit: mismo vector"
    assert "Medium" not in dyndolod_argv, "el preset lo elige el humano en el asistente; no viaja en el argv"
    assert "-game" not in texgen_argv and "-game" not in dyndolod_argv
    assert "--expert" not in dyndolod_argv
    assert "-t" not in texgen_argv and "-p" not in dyndolod_argv, "switches pelados: sin ':' se ignoran en silencio"
    assert not [a for a in dyndolod_argv if '"' in a], (
        "ningún elemento del argv lleva comillas: las pone list2cmdline, y embebirlas "
        "las mete en el VALOR de la ruta (rig 2026-08-10: 'Can not create path C:\\C:\\...')"
    )


async def test_dyndolod_config_default_omite_m_y_p(tmp_path: pathlib.Path) -> None:
    """Sin ``-m:``/``-p:`` explícitos el argv lleva solo ``-sse`` + ``-o:``/``-d:``/``-t:``.

    Derivar la carpeta Documentos a ciegas es frágil (OneDrive del rig), así que
    se omiten y la herramienta resuelve sus defaults por registro. ``-o:``/``-d:``/
    ``-t:`` siempre se emiten porque tienen default derivado de la config.
    """
    from sky_claw.local.tools.dyndolod_runner import DynDOLODConfig, DynDOLODRunner

    game = tmp_path / "Skyrim Special Edition"
    game.mkdir()
    exe_dir = tmp_path / "DynDOLOD"
    exe_dir.mkdir()
    dyndolod_exe = exe_dir / "DynDOLODx64.exe"
    dyndolod_exe.touch()

    runner = DynDOLODRunner(
        DynDOLODConfig(
            game_path=game,
            mo2_path=tmp_path / "MO2",
            mo2_mods_path=tmp_path / "MO2" / "mods",
            dyndolod_exe=dyndolod_exe,
        )
    )

    argv = runner._build_xedit_args(None)
    assert argv[0] == "-sse"
    assert len(argv) == 4, f"-sse + -o: + -d: + -t:, sin -m:/-p:; obtenido: {argv}"
    assert argv[1].startswith("-o:")
    assert argv[2].startswith("-d:")
    assert argv[3].startswith("-t:")
    assert all("-m:" not in a and "-p:" not in a for a in argv)
    assert not [a for a in argv if '"' in a], "los switches de ruta no llevan comillas embebidas"


def test_dyndolod_switch_de_ruta_formato_de_doc() -> None:
    """El formato ``-X:<ruta>`` con ``\\`` final en directorios es contrato, no estilo.

    Sin los dos puntos el parser de xEdit ignora el switch en silencio — así es
    como el defecto vivió sin dar señal. ``-p:`` es un archivo: sin ``\\`` final.

    Y **sin comillas**: este test las exigía, congelando el defecto que el rig
    encontró el 2026-08-10 (`INFORME_RIG_DYNDOLOD_ALPHA209.md` §P3). Las comillas
    de la doc son de la línea de comandos; en un elemento de argv pasan a ser
    parte del valor de la ruta y DynDOLOD muere con
    ``Can not create path C:\\C:\\...``. Quitar el ``\\`` final NO era el fix
    —``-p:`` no lo lleva y fallaba igual— y contradice al binario, que pide
    *"All path parameters must be specified with trailing backslash"*.
    """
    from sky_claw.local.tools.dyndolod_runner import DynDOLODRunner

    assert DynDOLODRunner._switch_de_ruta("t", pathlib.Path("TempTest"), directorio=True) == "-t:TempTest\\"
    assert DynDOLODRunner._switch_de_ruta("o", pathlib.Path("Salida"), directorio=True) == "-o:Salida\\"
    assert DynDOLODRunner._switch_de_ruta("p", pathlib.Path("plugins.txt"), directorio=False) == "-p:plugins.txt"


def _argv_segun_crt(cmdline: str) -> list[str]:
    """``cmdline`` parseada por ``shell32!CommandLineToArgvW`` — reglas de la CRT.

    Es el oráculo correcto para "¿mi serialización es válida como línea de comandos
    de Windows?" y **NO** para "¿qué recibe DynDOLOD?". DynDOLOD/TexGen son binarios
    Delphi y la RTL de Delphi no implementa el escape con backslash de la CRT: ver
    :func:`_argv_segun_delphi` y la divergencia anclada más abajo (hallazgo del
    revisor adversarial en el PR #462).
    """
    import ctypes
    from ctypes import wintypes

    fn = ctypes.windll.shell32.CommandLineToArgvW
    fn.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
    fn.restype = ctypes.POINTER(wintypes.LPWSTR)
    n = ctypes.c_int()
    puntero = fn(cmdline, ctypes.byref(n))
    assert puntero, f"CommandLineToArgvW falló sobre {cmdline!r}"
    try:
        return [puntero[i] for i in range(n.value)]
    finally:
        ctypes.windll.kernel32.LocalFree(puntero)


def _argv_segun_delphi(cmdline: str) -> list[str]:
    """``cmdline`` parseada con la semántica de ``System.pas::GetParamStr`` (Delphi).

    Dos diferencias con la CRT, y la segunda es la que importa:

    1. la comilla es un **delimitador** que se descarta, y puede abrir/cerrar en
       cualquier posición del token (de ahí que ``-o:"C:\\ruta\\"`` funcione);
    2. el backslash **nunca** es un carácter de escape. ``\\\\"`` no es "un
       backslash literal más comilla de cierre": son dos backslashes y una comilla.

    Reimplementación de referencia, no del binario real: se apoya en que
    ``DynDOLODx64.exe`` trae la RTL de Delphi (``ParamCount`` presente en sus
    strings) y en que xEdit lee sus parámetros con ``ParamStr``/``ParamCount``
    (``xeInit.pas``). Eso hace a esta función un oráculo **mucho** más cercano que
    ``CommandLineToArgvW``, pero sigue siendo INFERENCIA: el cierre autoritativo es
    T5 leyendo el ``Using Output Path:`` que el binario imprime en su log.
    """
    argv: list[str] = []
    i, n = 0, len(cmdline)
    while True:
        while i < n and cmdline[i] <= " ":
            i += 1
        while i + 1 < n and cmdline[i] == '"' and cmdline[i + 1] == '"':
            i += 2
        if i >= n:
            return argv
        token: list[str] = []
        while i < n and cmdline[i] > " ":
            if cmdline[i] == '"':
                i += 1
                while i < n and cmdline[i] != '"':
                    token.append(cmdline[i])
                    i += 1
                if i < n:
                    i += 1
            else:
                token.append(cmdline[i])
                i += 1
        argv.append("".join(token))


def _runner_con_raiz(tmp_path: pathlib.Path, carpeta: str, *, sin_espacios: bool = False) -> Any:
    """Runner con los cinco switches de ruta poblados bajo ``tmp_path/carpeta``.

    ``sin_espacios`` fuerza nombres sintéticos sin espacios en TODOS los tramos. Hace
    falta un flag porque los nombres reales (``Skyrim Special Edition`` del juego,
    ``My Games`` de Windows) traen espacios por sí mismos: en un rig de verdad no
    existe la variante sin espacios de ``-d:``/``-m:``, y esa es justamente la razón
    por la que la divergencia CRT↔Delphi no es un borde. Ver
    ``test_dyndolod_argv_con_espacios_diverge_entre_crt_y_delphi``.
    """
    from sky_claw.local.tools.dyndolod_runner import DynDOLODConfig, DynDOLODRunner

    nombre_juego = "SkyrimSE" if sin_espacios else "Skyrim Special Edition"
    nombre_inis = "MyGames" if sin_espacios else "My Games"

    raiz = tmp_path / carpeta
    game = raiz / nombre_juego
    (game / "Data").mkdir(parents=True)
    exe_dir = raiz / "DynDOLOD"
    exe_dir.mkdir()
    dyndolod_exe = exe_dir / "DynDOLODx64.exe"
    dyndolod_exe.touch()
    ini_dir = raiz / nombre_inis / nombre_juego
    ini_dir.mkdir(parents=True)
    plugins_file = raiz / "plugins.txt"
    plugins_file.touch()

    runner = DynDOLODRunner(
        DynDOLODConfig(
            game_path=game,
            mo2_path=raiz / "MO2",
            mo2_mods_path=raiz / "MO2" / "mods",
            dyndolod_exe=dyndolod_exe,
            texgen_exe=exe_dir / "TexGenx64.exe",
            data_dir=game / "Data",
            ini_dir=ini_dir,
            plugins_file=plugins_file,
            output_root=raiz / "salida",
            temp_dir=raiz / "temp",
        )
    )

    return runner, dyndolod_exe


@pytest.mark.skipif(sys.platform != "win32", reason="serialización de línea de comandos de Windows")
@pytest.mark.parametrize(
    ("etiqueta", "base", "duplica"),
    [
        ("sin espacios", "C:\\SkyClaw\\salida", False),
        ("con espacios", "C:\\Program Files\\Sky Claw\\salida", True),
        ("con paréntesis y espacios (Steam real)", "C:\\Program Files (x86)\\Steam\\Skyrim Special Edition", True),
    ],
)
def test_switch_de_ruta_round_trip_sintetico(etiqueta: str, base: str, duplica: bool) -> None:
    """Round-trip del helper sobre rutas SINTÉTICAS: sin filesystem, corre siempre.

    Existe porque los dos tests de abajo construyen su árbol bajo ``tmp_path``, que en
    Windows cuelga del perfil del usuario: en una máquina cuyo usuario tenga un espacio
    (``C:\\Users\\John Doe\\AppData\\Local\\Temp\\...``) la variante "sin espacios" es
    infabricable y el test se saltea. Este no depende del entorno —las rutas son
    literales y no necesitan existir— así que la propiedad queda cubierta en toda
    máquina Windows, incluida esa (hallazgo del revisor adversarial en el PR #462).

    Afirma las dos mitades del contrato medido:

    * la serialización es válida como línea de comandos de Windows (CRT round-trip);
    * Delphi ve un backslash final de más **sí y solo si** ``list2cmdline`` citó, o
      sea solo cuando la ruta trae espacios. ``duplica`` codifica esa expectativa por
      caso, así que un cambio de serialización que altere el patrón rompe el test en
      vez de pasar en silencio.
    """
    from sky_claw.local.tools.dyndolod_runner import DynDOLODRunner

    arg = DynDOLODRunner._switch_de_ruta("o", pathlib.Path(base), directorio=True)
    assert arg == f"-o:{base}\\"
    assert '"' not in arg

    exe = "C:\\t\\DynDOLODx64.exe"
    cmdline = subprocess.list2cmdline([exe, arg])
    crt = _argv_segun_crt(cmdline)[1]
    delphi = _argv_segun_delphi(cmdline)[1]

    assert crt == arg, f"[{etiqueta}] la serialización CRT debería cerrar: {cmdline}"
    esperado_delphi = arg + "\\" if duplica else arg
    assert delphi == esperado_delphi, (
        f"[{etiqueta}] Delphi no ve lo esperado.\n"
        f"  argv   : {arg!r}\n  cmdline: {cmdline}\n"
        f"  delphi : {delphi!r}\n  esperado: {esperado_delphi!r}\n"
        "Si la divergencia con espacios desapareció, releé la decisión A/B/C de T5."
    )


@pytest.mark.skipif(sys.platform != "win32", reason="round-trip contra el parser de línea de comandos de Windows")
def test_dyndolod_argv_sin_espacios_sobrevive_los_dos_parsers(tmp_path: pathlib.Path) -> None:
    """Ruta SIN espacios: ``list2cmdline`` no cita, y CRT y Delphi coinciden.

    Se saltea si la raíz de ``tmp_path`` trae espacios: en Windows el temp dir cuelga
    del perfil del usuario, así que con un usuario como ``John Doe`` la variante sin
    espacios es infabricable y el ``assert`` fallaría **por el entorno**, no por el
    código — un falso rojo en cualquier máquina de desarrollo con espacio en el nombre
    de usuario (CI no lo veía porque el runner usa ``runneradmin``). La propiedad no
    queda descubierta ahí: ``test_switch_de_ruta_round_trip_sintetico`` la cubre sin
    tocar el filesystem. Lo que este test aporta y ése no es la **enumeración de los
    cinco switches tal como los arma** ``_build_xedit_args``.

    Es el único caso de este archivo donde "el proceso recibe lo que Sky-Claw
    construyó" está afirmado de verdad: sin comillas externas no hay reglas de
    escape en juego, así que los dos parsers ven lo mismo y coincide con el argv.
    Además es exactamente el valor que el rig midió funcionando el 2026-08-10
    (``-o:C:\\...\\output\\`` vía línea verbatim).

    Con las comillas embebidas de la implementación anterior, el rig midió que
    **ninguna** corrida podía arrancar (2/2 binarios,
    ``Can not create path C:\\C:\\...``) y los tres tests de argv que ya había
    estaban en verde: los tres miraban el string que Sky-Claw produce, ninguno lo
    que el proceso recibe. Este enumera los cinco switches de ruta a la vez.
    """
    if " " in str(tmp_path):
        pytest.skip(
            f"la raíz temporal trae espacios ({tmp_path}): la variante sin espacios es "
            "infabricable acá. La cubre test_switch_de_ruta_round_trip_sintetico."
        )

    runner, dyndolod_exe = _runner_con_raiz(tmp_path, "Sky-Claw", sin_espacios=True)

    argv = runner._build_xedit_args(None)
    assert len(argv) == 6, f"-sse + los cinco switches de ruta; obtenido: {argv}"
    assert not [a for a in argv if " " in a], "este caso exige rutas sin espacios"

    cmdline = subprocess.list2cmdline([str(dyndolod_exe), *argv])
    assert '"' not in cmdline[len(str(dyndolod_exe)) + 2 :], (
        f"sin espacios `list2cmdline` no debería citar nada: {cmdline}"
    )

    for nombre, parser in (("CRT", _argv_segun_crt), ("Delphi", _argv_segun_delphi)):
        recibido = parser(cmdline)[1:]
        assert recibido == argv, (
            f"[{nombre}] el proceso no recibe el argv que Sky-Claw construyó.\n"
            f"  construido: {argv}\n  cmdline   : {cmdline}\n  recibido  : {recibido}"
        )
    assert not [a for a in argv if '"' in a]


@pytest.mark.skipif(sys.platform != "win32", reason="serialización de línea de comandos de Windows")
def test_dyndolod_argv_con_espacios_diverge_entre_crt_y_delphi(tmp_path: pathlib.Path) -> None:
    """Ruta CON espacios: los dos parsers **no** ven lo mismo. Pendiente de T5.

    Hallazgo del revisor adversarial en el PR #462, confirmado por medición:
    ``list2cmdline`` cita solo si el argumento tiene espacios, y al citar **duplica
    el backslash final** para que no escape su propia comilla de cierre. Eso es
    correcto bajo las reglas de la CRT y **no** bajo las de Delphi, que no trata el
    backslash como escape:

    ======================  ==========================================
    intención                ``-o:C:\\Program Files\\...\\salida\\``
    ``list2cmdline``         ``"-o:C:\\Program Files\\...\\salida\\\\"``
    CRT ve                   ``-o:C:\\Program Files\\...\\salida\\``
    **Delphi ve**            ``-o:C:\\Program Files\\...\\salida\\\\``
    ======================  ==========================================

    **Y este es el caso NORMAL, no un borde. En un rig real es inevitable**: el juego
    se instala en una carpeta llamada ``Skyrim Special Edition`` y los INI viven bajo
    ``My Games``, así que ``-d:`` y ``-m:`` traen espacios sí o sí, sin importar dónde
    el operador ponga la salida (y el path típico agrega
    ``C:\\Program Files (x86)\\Steam\\steamapps\\common\\`` encima). La variante sin
    espacios de este argv solo existe con nombres sintéticos — de hecho el fixture
    necesita un flag para fabricarla, y ese detalle es la evidencia: la rama que este
    test ancla es la que corre siempre.

    Este test **no afirma que el vector sea correcto** —eso es justo lo que no está
    verificado— sino que ancla la divergencia exacta que hoy existe, para que:

    * nadie la lea como verificada (el test anterior la daba por buena);
    * si alguien cambia la serialización y los dos parsers pasan a coincidir, este
      test se ponga ROJO y obligue a releer la decisión en vez de dejarla pasar.

    Lo resuelve **T5** con una corrida contra Alpha-209 real: el binario ecoa la
    ruta que parseó en su log (``Using Output Path: …``), así que una sola corrida
    con un ``-o:`` que contenga espacios decide entre las tres formas candidatas
    (dejar el backslash duplicado si el binario lo tolera / no emitir el ``\\``
    final / línea verbatim). Hasta entonces queda declarado, no supuesto.
    """
    runner, dyndolod_exe = _runner_con_raiz(tmp_path, "Program Files Sky Claw")

    argv = runner._build_xedit_args(None)
    con_espacios = [a for a in argv if " " in a]
    assert con_espacios, "este caso exige rutas con espacios"

    cmdline = subprocess.list2cmdline([str(dyndolod_exe), *argv])
    crt = _argv_segun_crt(cmdline)[1:]
    delphi = _argv_segun_delphi(cmdline)[1:]

    # La serialización SÍ es válida como línea de comandos de Windows.
    assert crt == argv, f"la serialización CRT debería cerrar: {cmdline}"

    # Y Delphi ve un backslash final de más en cada switch de directorio citado.
    assert delphi != argv, (
        "si Delphi y el argv coinciden, la divergencia medida en el PR #462 dejó de "
        "existir: releé la decisión A/B/C y actualizá T5 en vez de borrar este test"
    )
    esperado_delphi = [a + "\\" if (" " in a and a.endswith("\\")) else a for a in argv]
    assert delphi == esperado_delphi, (
        "la divergencia cambió de forma; se esperaba solo el backslash final duplicado "
        f"en los switches citados.\n  argv  : {argv}\n  delphi: {delphi}"
    )


@pytest.mark.parametrize("lanzador", ["run_texgen", "run_dyndolod"])
async def test_dyndolod_extra_args_con_comillas_es_rechazado(tmp_path: pathlib.Path, lanzador: str) -> None:
    """``extra_args`` no puede evadir ``_switch_de_ruta``: fail-closed.

    Es la puerta hermana del defecto de este PR. ``_build_xedit_args`` extendía
    ``extra_args`` **verbatim**, así que un ``-o:"C:\\...\\"`` que entrara por ahí
    reproducía el ``Can not create path C:\\C:\\...`` sin pasar por el helper que
    acabamos de arreglar (hallazgo de CodeRabbit en el PR #462).

    Y es alcanzable desde payload: ``GenerateLodsStrategy._VALID_LOD_KEYS`` incluye
    ``texgen_args`` y ``dyndolod_args``, y ese allowlist filtra la CLAVE sin mirar
    el VALOR.

    Enumera **los dos** lanzadores: validar en uno y no en el otro es exactamente
    el defecto que `AGENTS.md` llama dominante (#373). La validación vive en la
    pieza compartida, así que ambos la heredan por construcción — y este test lo
    verifica en vez de asumirlo.
    """
    from sky_claw.local.tools.dyndolod_runner import (
        DynDOLODConfig,
        DynDOLODRunner,
        DynDOLODValidationError,
    )

    game = tmp_path / "Skyrim Special Edition"
    (game / "Data").mkdir(parents=True)
    exe_dir = tmp_path / "DynDOLOD"
    exe_dir.mkdir()
    dyndolod_exe = exe_dir / "DynDOLODx64.exe"
    dyndolod_exe.touch()
    texgen_exe = exe_dir / "TexGenx64.exe"
    texgen_exe.touch()

    runner = DynDOLODRunner(
        DynDOLODConfig(
            game_path=game,
            mo2_path=tmp_path / "MO2",
            mo2_mods_path=tmp_path / "MO2" / "mods",
            dyndolod_exe=dyndolod_exe,
            texgen_exe=texgen_exe,
        )
    )

    with patch.object(runner, "_execute_process", AsyncMock(return_value=("", "", 0, 1.0))) as ejecutar:
        with pytest.raises(DynDOLODValidationError, match="comillas"):
            await getattr(runner, lanzador)(extra_args=['-o:"C:\\otra\\ruta\\"'])
        assert not ejecutar.called, "el proceso no debe lanzarse con un argv inválido"

    # Un extra_args limpio sigue pasando: la validación rechaza comillas, no el
    # mecanismo de argumentos extra.
    argv = runner._build_xedit_args(["-B:C:\\backups\\"])
    assert argv[-1] == "-B:C:\\backups\\"


@pytest.mark.parametrize("letra", list(_LETRAS_ADMINISTRADAS))
@pytest.mark.parametrize("lanzador", ["run_texgen", "run_dyndolod"])
async def test_dyndolod_extra_args_no_puede_redefinir_un_switch_administrado(
    tmp_path: pathlib.Path,
    lanzador: str,
    letra: str,
) -> None:
    """Los switches de ruta tienen dueño: ``DynDOLODConfig``.

    Rechazar solo las comillas no alcanzaba: un ``-o:C:\\otra\\ruta\\`` sin comillas
    pasaba y **pisaba la raíz administrada**, creando dos fuentes de verdad. El
    staging, el post-check (``_candidatos_de_salida`` mira bajo ``output_root``) y el
    rollback asumen que la salida está donde la config dice; y el parser de xEdit no
    documenta cuál gana si el switch aparece dos veces, así que el resultado sería
    indeterminado además de ambiguo (review del PR #462).

    Enumera **todos** los switches × **los dos** lanzadores: un caso escrito a mano
    para el que uno se acordó no ataja al de al lado. La lista de letras sale de
    ``_LETRAS_ADMINISTRADAS``, no de un literal en este archivo — un literal acá
    sería una tercera copia que puede desalinearse del código sin dar señal. Que
    esa constante siga cubriendo lo que el runner emite lo verifica
    ``test_letras_administradas_cubre_los_switches_que_emite_el_runner``.
    """
    from sky_claw.local.tools.dyndolod_runner import DynDOLODValidationError

    runner, _ = _runner_con_raiz(tmp_path, "Sky-Claw")

    with patch.object(runner, "_execute_process", AsyncMock(return_value=("", "", 0, 1.0))) as ejecutar:
        for variante in (f"-{letra}:C:\\otra\\ruta\\", f"-{letra.upper()}:C:\\otra\\ruta\\"):
            with pytest.raises(DynDOLODValidationError, match="administra DynDOLODConfig"):
                await getattr(runner, lanzador)(extra_args=[variante])
        assert not ejecutar.called


@pytest.mark.parametrize("letra", list(_LETRAS_ADMINISTRADAS))
@pytest.mark.parametrize("prefijo", ["-", "/", " -", "\t-", "  /"])
async def test_dyndolod_extra_args_rechaza_las_formas_alternativas_del_prefijo(
    tmp_path: pathlib.Path,
    prefijo: str,
    letra: str,
) -> None:
    """El rechazo no se evade cambiando ``-`` por ``/`` ni metiendo un espacio.

    La RTL de Delphi reconoce ``/switch`` en ``FindCmdLineSwitch`` y NO está
    verificado cuál de las dos formas mira el parser de xEdit — así que la regla
    resuelve la duda fail-closed, igual que el resto de ``_extra_args_admisibles``.
    La asimetría es lo que decide: rechazar un ``/o:`` que el binario tal vez ignore
    no le cuesta nada a ningún caller legítimo (la doc usa ``-`` y nadie emite la
    otra forma), mientras que dejar pasar uno que sí honre pisa la raíz administrada.

    Con la regex anclada en ``^-`` que tenía la primera versión, las cinco variantes
    de prefijo de este test pasaban derecho al argv (review del PR #462).
    """
    from sky_claw.local.tools.dyndolod_runner import DynDOLODValidationError

    runner, _ = _runner_con_raiz(tmp_path, "Sky-Claw")

    with patch.object(runner, "_execute_process", AsyncMock(return_value=("", "", 0, 1.0))) as ejecutar:
        with pytest.raises(DynDOLODValidationError, match="administra DynDOLODConfig"):
            await runner.run_dyndolod(extra_args=[f"{prefijo}{letra}:C:\\otra\\ruta\\"])
        assert not ejecutar.called


def test_letras_administradas_cubre_los_switches_que_emite_el_runner() -> None:
    """``_LETRAS_ADMINISTRADAS`` y la tupla ``switches`` son la misma lista, dos veces.

    Nada en el lenguaje las acopla: son dos literales en el mismo módulo. Este ancla
    lee la tupla **por AST** y exige igualdad literal, que es la diferencia entre
    enumerar y muestrear (`AGENTS.md`).

    Sin él, agregar un sexto switch administrado a ``_build_xedit_args`` lo dejaba
    fuera de ``_extra_args_admisibles`` y ``extra_args`` podía pisarlo. Medido por
    mutación en la review del PR #462: los tests de vector solo se quejaban del
    ``len`` del argv, y actualizar ese ``len`` —lo que haría cualquiera al agregar un
    switch— devolvía la suite entera a verde con el hueco abierto. El comentario del
    módulo llegó a afirmar que las letras "se derivan" de la tupla; no se derivaban,
    y esa afirmación es peor que ninguna porque suprime la auditoría del próximo.
    """
    import inspect
    import textwrap

    from sky_claw.local.tools import dyndolod_runner

    fuente = textwrap.dedent(inspect.getsource(dyndolod_runner.DynDOLODRunner._build_xedit_args))
    asignaciones = [
        nodo
        for nodo in ast.walk(ast.parse(fuente))
        if isinstance(nodo, ast.Assign) and any(getattr(t, "id", None) == "switches" for t in nodo.targets)
    ]
    assert len(asignaciones) == 1, (
        f"se esperaba exactamente una asignación de `switches` en `_build_xedit_args`; hay {len(asignaciones)}. "
        "Si el runner cambió de forma, este ancla necesita actualizarse — no borrarse."
    )

    emitidas = {elemento.elts[0].value for elemento in asignaciones[0].value.elts}
    assert emitidas == set(_LETRAS_ADMINISTRADAS), (
        f"`_build_xedit_args` emite {sorted(emitidas)} y el rechazo cubre "
        f"{sorted(set(_LETRAS_ADMINISTRADAS))}. Un switch administrado quedó fuera de "
        "`_extra_args_admisibles`: `extra_args` puede pisarlo y crear dos fuentes de verdad "
        "sobre una ruta que el staging, el post-check y el rollback dan por fija."
    )


@pytest.mark.parametrize("lanzador", ["run_texgen", "run_dyndolod"])
async def test_dyndolod_extra_args_rechaza_tipos_invalidos(tmp_path: pathlib.Path, lanzador: str) -> None:
    """``extra_args`` viene de payload: el type hint no valida en runtime.

    ``GenerateLodsStrategy._filter_payload`` deja pasar ``texgen_args``/
    ``dyndolod_args`` filtrando la CLAVE, sin mirar tipo ni valor. Un ``str`` suelto
    se extendería **carácter por carácter** sobre el argv, que es el modo más
    silencioso de corromperlo.
    """
    from sky_claw.local.tools.dyndolod_runner import DynDOLODValidationError

    runner, _ = _runner_con_raiz(tmp_path, "Sky-Claw")

    for invalido in ("-B:C:\\backups\\", {"-B": "x"}, [1, 2], ["-B:ok", None]):
        with patch.object(runner, "_execute_process", AsyncMock(return_value=("", "", 0, 1.0))) as ejecutar:
            with pytest.raises(DynDOLODValidationError, match=r"list\[str\]"):
                await getattr(runner, lanzador)(extra_args=invalido)  # type: ignore[arg-type]
            assert not ejecutar.called


async def test_generate_lods_no_puede_inyectar_switches_por_payload(tmp_path: pathlib.Path) -> None:
    """El boundary real: ``payload → GenerateLodsStrategy → service → runner``.

    Es la ruta que hace relevante el problema y la que ningún test cruzaba. Los tests
    de arriba entran por el runner; este entra por donde entra un payload de verdad,
    porque ``_VALID_LOD_KEYS`` incluye ``texgen_args``/``dyndolod_args`` y el
    allowlist filtra la clave sin mirar el valor.

    Verifica las dos mitades del contrato: la clave sigue siendo aceptada (no se
    rompió la API) y el valor ofensivo es rechazado antes de lanzar nada.
    """
    from sky_claw.app.orchestrator.tool_strategies.generate_lods import (
        _VALID_LOD_KEYS,
        GenerateLodsStrategy,
    )
    from sky_claw.local.tools.dyndolod_runner import DynDOLODValidationError

    assert {"texgen_args", "dyndolod_args"} <= _VALID_LOD_KEYS, (
        "si estas claves dejan de estar permitidas, este boundary cambió: revisá el test"
    )

    runner, _ = _runner_con_raiz(tmp_path, "Sky-Claw")

    class _ServicioFalso:
        """Stub del servicio: reenvía al runner como hace `DynDOLODPipelineService`."""

        async def execute(self, **kwargs: Any) -> dict[str, Any]:
            return {"argv": runner._build_xedit_args(kwargs.get("dyndolod_args"))}

    estrategia = GenerateLodsStrategy(_ServicioFalso())  # type: ignore[arg-type]

    for ofensivo in (['-o:"C:\\evil\\"'], ["-o:C:\\evil\\"], "-o:C:\\evil\\"):
        with pytest.raises(DynDOLODValidationError):
            await estrategia.execute({"preset": "Medium", "dyndolod_args": ofensivo})

    # Un extra legítimo sigue cruzando el boundary intacto.
    resultado = await estrategia.execute({"dyndolod_args": ["-B:C:\\backups\\"]})
    assert resultado["argv"][-1] == "-B:C:\\backups\\"


@pytest.mark.skipif(sys.platform != "win32", reason="normalización de rutas de Windows")
def test_dyndolod_switch_de_ruta_no_duplica_el_backslash_de_una_raiz() -> None:
    """``C:\\`` ya termina en backslash: agregarle otro es una ruta distinta.

    Es la única forma en que ``str(pathlib.Path(...))`` llega con separador final
    (pathlib normaliza el resto), así que es la rama real del ``endswith`` — y la
    que un fix apresurado que hiciera ``texto + "\\\\"`` sin condición rompería.
    """
    from sky_claw.local.tools.dyndolod_runner import DynDOLODRunner

    assert DynDOLODRunner._switch_de_ruta("o", pathlib.Path("C:\\"), directorio=True) == "-o:C:\\"


async def test_dyndolod_modo_vr_se_fija_por_config_no_por_ruta(tmp_path: pathlib.Path) -> None:
    """El game mode es un campo tipado de la config, no una heurística de ruta.

    La heurística ("VR" en el string del path) vivía duplicada en el runner y un
    path con "VR" (CVR, VRam) volcaba un SSE a ``-tes5vr`` en silencio. El campo
    se resuelve UNA vez en ``__post_init__`` (default retrocompatible para
    instalaciones VR reales) y los consumidores —argv y nombre del log— lo leen.
    """
    from sky_claw.local.tools.dyndolod_runner import DynDOLODConfig, DynDOLODRunner

    exe_dir = tmp_path / "DynDOLOD"
    exe_dir.mkdir()
    exe = exe_dir / "DynDOLODx64.exe"
    exe.touch()

    def _runner(game: pathlib.Path, game_mode: str | None) -> DynDOLODRunner:
        game.mkdir(parents=True)
        return DynDOLODRunner(
            DynDOLODConfig(
                game_path=game,
                mo2_path=tmp_path / "MO2",
                mo2_mods_path=tmp_path / "MO2" / "mods",
                dyndolod_exe=exe,
                game_mode=game_mode,
            )
        )

    # SSE explícito aunque el path contenga "VR": la ruta NO decide.
    sse = _runner(tmp_path / "a" / "CVR" / "Skyrim Special Edition", "sse")
    assert sse._build_xedit_args(None)[0] == "-sse"
    assert sse._modo() == "SSE"

    # VR explícito: switch suelto y nombre de log TES5VR.
    vr = _runner(tmp_path / "b" / "Skyrim VR", "tes5vr")
    assert vr._build_xedit_args(None)[0] == "-tes5vr"
    assert vr._modo() == "TES5VR"

    # Default inferido: instalaciones reales de VR se detectan solas, por el
    # NOMBRE de la carpeta del juego (con o sin espacio).
    for nombre in ("Skyrim VR", "SkyrimVR"):
        inferido_vr = _runner(tmp_path / f"vr-{nombre.replace(' ', '')}" / nombre, None)
        assert inferido_vr._build_xedit_args(None)[0] == "-tes5vr"
        assert inferido_vr._modo() == "TES5VR"

    # Y el caso que el docstring del runner nombra como el bug: una instalación
    # SSE colgada de un directorio con "VR" en el string, SIN override. El
    # substring sobre la ruta entera la volcaba a -tes5vr en silencio, y de paso
    # mandaba a `_modo()` a buscar un log *_TES5VR_log.txt que nunca existe:
    # el post-check se quedaba sin evidencia y degradaba a artefacto-solo.
    # Instalaciones VR reales con nombre NO canónico: exigir exactamente
    # "SkyrimVR" cambiaba el falso positivo de abajo por un falso NEGATIVO —caían
    # a -sse en silencio, generando LODs del mundo equivocado sobre datos VR
    # (review adversarial #441). Se piden las dos marcas en el nombre.
    for vr_no_canonico in ("Skyrim-VR", "VR Skyrim", "Skyrim VR - portable", "SkyrimVR Modded"):
        inferido = _runner(tmp_path / f"nc-{vr_no_canonico.replace(' ', '_')}" / vr_no_canonico, None)
        assert inferido._build_xedit_args(None)[0] == "-tes5vr", f"{vr_no_canonico} es una instalación VR"
        assert inferido._modo() == "TES5VR"

    # Y los ambiguos DENTRO del nombre de la carpeta del juego: comparar por
    # substring sobre el nombre casaba `Skyrim CVR` y `Skyrim VRamDisk Edition`
    # — el mismo error del substring sobre la ruta, más chico (review #441).
    for sse_ambiguo in ("Skyrim CVR", "Skyrim VRamDisk Edition", "Skyrim SE - CVR build"):
        inferido = _runner(tmp_path / f"amb-{sse_ambiguo.replace(' ', '_')}" / sse_ambiguo, None)
        assert inferido._build_xedit_args(None)[0] == "-sse", f"{sse_ambiguo} no tiene `vr` como token"
        assert inferido._modo() == "SSE"

    for ambiguo in ("CVR", "VRamDisk", "SteamVR"):
        inferido_sse = _runner(tmp_path / ambiguo / "Skyrim Special Edition", None)
        assert inferido_sse._build_xedit_args(None)[0] == "-sse", f"{ambiguo} no es un marcador de VR"
        assert inferido_sse._modo() == "SSE"


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

"""Contrato #601: la carpeta declarada para ``-m:`` debe tener la INI del game mode.

``-m:`` (``DYNDLOD_INI_DIR``) es la BASE sobre la que el core heredado de xEdit
compone el archivo que va a abrir: ``<carpeta>\\<wbGameName>.ini``. Antes de #601
alcanzaba con que la carpeta fuera un directorio existente (lo único que valida
``PathResolutionService.get_dyndolod_ini_dir``), así que una carpeta vacía —o con
la INI de otro juego— satisfacía el prerequisito y la corrida moría DESPUÉS del
spawn con ``Fatal: Could not find ini`` (o, en modo VR, usaba la INI de otra
ubicación por el fallback del binario).

**Procedencia del nombre del archivo** (FASE 1 de #601, verificada, no inferida):

* ``xEdit/xeInit.pas`` del repo oficial ``TES5Edit/TES5Edit`` (rama
  ``dev-4.1.6`` — el core que DynDOLOD/TexGen heredan):
  ``wbTheGameIniFileName := wbMyGamesTheGamePath + wbGameName + '.ini'``, con
  ``wbGameName := 'Skyrim'`` en los casos ``TES5VR`` **y** ``SSE``;
  ``wbGameName2`` ('Skyrim VR' / 'Skyrim Special Edition') sólo nombra la carpeta
  ``Documents\\My Games\\<...>`` y NO entra en el nombre del archivo. Los modos VR
  tienen además un fallback explícito (comentario del propio fuente: *"VR games
  don't create ini file in My Games by default, use the one in the game folder"*)
  que reemplaza la carpeta declarada por el directorio del juego cuando ese
  archivo no existe.
* Doc oficial: ``dyndolod.info/Help/Command-Line-Argument`` describe ``-m:`` como
  *"path to INI folder"* sin nombrar archivo; la página de Skyrim VR declara que
  TES5VR usa los configs con el identificador **SSE**.
* Rig local con la versión soportada (DynDOLOD 3.0 Alpha-209 x64, 2026-09-20,
  ``-m:``/``-d:`` sintéticos): SSE + carpeta con ``Skyrim.ini`` →
  ``Using ini: ...\\Skyrim.ini``; TES5VR + la MISMA carpeta → ``Using ini:
  ...\\Skyrim.ini``; TES5VR + carpeta con sólo ``SkyrimVR.ini`` → NO la usa:
  busca ``Skyrim.ini``, cae al fallback del directorio del juego y muere con
  ``Fatal: Could not find ini`` / ``Skyrim.ini can not be found … Current game
  mode: Skyrim VR (TES5VR)``. Que ``SkyrimPrefs.ini`` NO sea requisito de
  arranque también está medido (una carpeta con SOLO ``Skyrim.ini`` arranca el
  background loader igual).

**Lo que esto NO es**: la expectativa de que SSE y TES5VR necesiten archivos de
nombre distinto no se cumple en esta versión — los dos requieren ``Skyrim.ini``—,
así que "la INI del modo incorrecto" no se expresa con dos nombres válidos
alternativos. Lo que sí se expresa, y es lo que estos tests congelan, es que un
nombre plausible-pero-falso (``SkyrimVR.ini``, ``SkyrimSE.ini``) o una INI que no
es la primaria (``SkyrimPrefs.ini``) NO satisfacen a NINGÚN modo.
"""

from __future__ import annotations

import ast
import inspect
import pathlib
import textwrap
from typing import Literal

import pytest

from sky_claw.local.tools.dyndolod_runner import (
    _INI_PRIMARIA_POR_GAME_MODE,
    DynDOLODConfig,
    DynDOLODRunner,
    DynDOLODValidationError,
    ReadinessMode,
)
from sky_claw.local.tools.output_targets import HerramientaDynDOLOD
from tests._symlink_guard import symlink_guard

#: Nombre de carpeta de juego real por modo. Para ``sse`` se usa el que ya usan
#: los tests de #597; para ``tes5vr``, la forma canónica que el binario reporta
#: como *"Skyrim VR"* (y que ``__post_init__`` usa para inferir el modo).
_NOMBRE_DE_JUEGO_POR_MODO: dict[str, str] = {
    "sse": "Skyrim Special Edition",
    "tes5vr": "Skyrim VR",
}


def _arbol_minimo(tmp_path: pathlib.Path, *, nombre_juego: str) -> tuple[pathlib.Path, pathlib.Path]:
    """``(game_path, dyndolod_exe)`` mínimos: lo que ``__post_init__`` ya exigía."""
    game = tmp_path / nombre_juego
    game.mkdir(parents=True, exist_ok=True)
    exe = tmp_path / "DynDOLOD" / "DynDOLODx64.exe"
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.touch()
    return game, exe


def _config(
    tmp_path: pathlib.Path,
    *,
    game: pathlib.Path,
    exe: pathlib.Path,
    modo: Literal["sse", "tes5vr"] | None,
    ini_dir: pathlib.Path | None,
) -> DynDOLODConfig:
    return DynDOLODConfig(
        game_path=game,
        mo2_path=tmp_path / "MO2",
        mo2_mods_path=tmp_path / "MO2" / "mods",
        dyndolod_exe=exe,
        ini_dir=ini_dir,
        game_mode=modo,
    )


def _ini_dir_con(tmp_path: pathlib.Path, *, nombre: str, contenido: tuple[str, ...]) -> pathlib.Path:
    """Carpeta declarada para ``-m:`` con los archivos pedidos (vacía si no hay)."""
    ini_dir = tmp_path / "My Games" / nombre
    ini_dir.mkdir(parents=True, exist_ok=True)
    for nombre_archivo in contenido:
        (ini_dir / nombre_archivo).write_text("[General]\n", encoding="utf-8")
    return ini_dir


def test_la_tabla_de_ini_primaria_esta_congelada_e_es_inmutable() -> None:
    """La tabla por modo es un literal cerrado, no una heurística.

    Igualdad literal (no ``in``/``issubset``): agregar un modo o cambiar el
    archivo requerido exige pasar por acá y decidir la fuente. La inmutabilidad
    también se ejerce: un ``dict`` mutable invitaría a "parchearlo" en runtime
    desde otro módulo.
    """
    assert dict(_INI_PRIMARIA_POR_GAME_MODE) == {"sse": "Skyrim.ini", "tes5vr": "Skyrim.ini"}

    with pytest.raises(TypeError):
        _INI_PRIMARIA_POR_GAME_MODE["sse"] = "otra.ini"  # type: ignore[index]


def test_la_tabla_cubre_los_game_modes_que_el_runner_puede_emitir() -> None:
    """Todo modo emitible en el argv tiene entrada en la tabla. Por AST, no a mano.

    Misma técnica que ``test_game_modes_administrados_cubre_los_que_emite_el_runner``
    (``test_contrato_argumentos_cli.py``): el conjunto de modos soportados y el de
    la tabla son dos literales del mismo módulo que nada acopla. Este ancla lee los
    strings del ``game_mode = "-tes5vr" if … else "-sse"`` de ``_build_xedit_args``
    y exige contención en la tabla: un modo nuevo emitible sin decisión de INI
    primaria rompe acá en vez de heredar el archivo de otro modo en silencio.
    """
    fuente = textwrap.dedent(inspect.getsource(DynDOLODRunner._build_xedit_args))
    asignaciones = [
        nodo
        for nodo in ast.walk(ast.parse(fuente))
        if isinstance(nodo, ast.Assign) and any(getattr(t, "id", None) == "game_mode" for t in nodo.targets)
    ]
    assert len(asignaciones) == 1, (
        f"se esperaba exactamente una asignación de `game_mode` en `_build_xedit_args`; hay {len(asignaciones)}. "
        "Si el runner cambió de forma, este ancla necesita actualizarse — no borrarse."
    )

    emitidos = {
        nodo.value.lstrip("-/").lower()
        for nodo in ast.walk(asignaciones[0].value)
        if isinstance(nodo, ast.Constant) and isinstance(nodo.value, str)
    }
    assert emitidos, "no se extrajo ningún game mode del AST: el ancla dejó de leer lo que cree leer"
    assert emitidos <= set(_INI_PRIMARIA_POR_GAME_MODE), (
        f"`_build_xedit_args` puede emitir {sorted(emitidos)} y la tabla de INI primaria cubre "
        f"{sorted(_INI_PRIMARIA_POR_GAME_MODE)}: un modo emitible quedó sin decidir su INI requerida."
    )


@pytest.mark.parametrize("modo", ["sse", "tes5vr"])
@pytest.mark.parametrize(
    "contenido",
    [
        pytest.param(("Skyrim.ini",), id="solo-la-primaria"),
        pytest.param(("Skyrim.ini", "SkyrimPrefs.ini"), id="layout-real-de-My-Games"),
        pytest.param(
            ("Skyrim.ini", "SkyrimPrefs.ini", "SkyrimCustom.ini"),
            id="con-custom-ini",
        ),
    ],
)
def test_ini_dir_con_la_ini_del_modo_es_valido(
    tmp_path: pathlib.Path,
    modo: Literal["sse", "tes5vr"],
    contenido: tuple[str, ...],
) -> None:
    """Positivo de los dos modos: la carpeta con ``Skyrim.ini`` es una declaración válida.

    El caso ``solo-la-primaria`` es el que además verifica, por comportamiento,
    que ``SkyrimPrefs.ini`` NO es requisito: con sólo ``Skyrim.ini`` la config se
    construye. El binario arranca igual en el rig — no lo estamos exigiendo "por
    las dudas", que es como los prerequisitos inventados llegan a producción.
    """
    game, exe = _arbol_minimo(tmp_path, nombre_juego=_NOMBRE_DE_JUEGO_POR_MODO[modo])
    ini_dir = _ini_dir_con(tmp_path, nombre=_NOMBRE_DE_JUEGO_POR_MODO[modo], contenido=contenido)

    config = _config(tmp_path, game=game, exe=exe, modo=modo, ini_dir=ini_dir)

    assert config.ini_dir == ini_dir
    assert config.game_mode == modo, "el modo efectivo es el de la tabla que se acaba de validar"
    runner = DynDOLODRunner(config, readiness=ReadinessMode.DISABLED_FOR_TEST)
    m = [a for a in runner._build_xedit_args(None, herramienta=HerramientaDynDOLOD.DYNDOLOD) if a.startswith("-m:")]
    assert m == [f"-m:{ini_dir}\\"], "la carpeta validada sigue viajando como -m: (con \\ final)"


@pytest.mark.parametrize("modo", ["sse", "tes5vr"])
def test_game_mode_inferido_del_nombre_del_juego_usa_la_misma_tabla(
    tmp_path: pathlib.Path,
    modo: Literal["sse", "tes5vr"],
) -> None:
    """Sin ``game_mode`` explícito, la validación usa el modo INFERIDO, no "sse".

    Es la mitad que un default de fábrica escondería: ``_ensure_runner`` no pasa
    ``game_mode``, así que el modo efectivo sale del nombre de la carpeta del juego
    y la tabla tiene que leerse DESPUÉS de esa decisión — si no, una declaración
    de VR se validaría contra la tabla de SSE (hoy el archivo coincide, pero la
    propiedad que se pregunta es "el requerido por el modo EFECTIVO").
    """
    game, exe = _arbol_minimo(tmp_path, nombre_juego=_NOMBRE_DE_JUEGO_POR_MODO[modo])
    ini_dir = _ini_dir_con(tmp_path, nombre="declarada", contenido=("Skyrim.ini",))

    config = _config(tmp_path, game=game, exe=exe, modo=None, ini_dir=ini_dir)

    assert config.game_mode == modo


@pytest.mark.parametrize("modo", ["sse", "tes5vr"])
@pytest.mark.parametrize(
    "contenido",
    [
        pytest.param((), id="carpeta-vacia"),
        pytest.param(("SkyrimVR.ini",), id="nombre-de-VR"),
        pytest.param(("SkyrimSE.ini",), id="nombre-del-exe-de-SSE"),
        pytest.param(("SkyrimPrefs.ini",), id="solo-la-secundaria"),
        pytest.param(("SkyrimPrefs.ini", "SkyrimVR.ini"), id="secundaria-mas-nombre-falso"),
    ],
)
def test_ini_dir_sin_la_ini_del_modo_falla_cerrado(
    tmp_path: pathlib.Path,
    modo: Literal["sse", "tes5vr"],
    contenido: tuple[str, ...],
) -> None:
    """Negativo de los dos modos: sin la INI primaria, la config NO existe.

    ``SkyrimPrefs.ini`` sola entra a propósito: es la INI que un "¿está la carpeta
    de configs del juego?" aproximaría por nombre, y el binario no la usa como
    primaria — con sólo ella la corrida muere igual. ``SkyrimVR.ini`` es el nombre
    que un mapeo inventado por modo habría exigido en VR: el rig midió que el
    binario busca ``Skyrim.ini`` y no levanta con esa carpeta.
    """
    game, exe = _arbol_minimo(tmp_path, nombre_juego=_NOMBRE_DE_JUEGO_POR_MODO[modo])
    ini_dir = _ini_dir_con(tmp_path, nombre=_NOMBRE_DE_JUEGO_POR_MODO[modo], contenido=contenido)

    with pytest.raises(DynDOLODValidationError) as excinfo:
        _config(tmp_path, game=game, exe=exe, modo=modo, ini_dir=ini_dir)

    mensaje = str(excinfo.value)
    assert modo in mensaje, f"el mensaje tiene que nombrar el game mode efectivo: {mensaje!r}"
    assert str(ini_dir) in mensaje, f"el mensaje tiene que nombrar la carpeta declarada: {mensaje!r}"
    assert "Skyrim.ini" in mensaje, f"el mensaje tiene que nombrar el archivo esperado: {mensaje!r}"


@pytest.mark.parametrize("modo", ["sse", "tes5vr"])
def test_un_directorio_llamado_como_la_ini_no_satisface(
    tmp_path: pathlib.Path,
    modo: Literal["sse", "tes5vr"],
) -> None:
    """Un DIRECTORIO llamado ``Skyrim.ini`` no es "un archivo regular utilizable".

    ``Path.is_file()`` es la primitiva del contrato (no ``exists()``): el binario
    abre un archivo, y el modo de falla que esto cierra es la carpeta homónima
    que ``exists()`` habría aceptado.
    """
    game, exe = _arbol_minimo(tmp_path, nombre_juego=_NOMBRE_DE_JUEGO_POR_MODO[modo])
    ini_dir = _ini_dir_con(tmp_path, nombre=_NOMBRE_DE_JUEGO_POR_MODO[modo], contenido=())
    (ini_dir / "Skyrim.ini").mkdir()

    with pytest.raises(DynDOLODValidationError, match="Skyrim.ini"):
        _config(tmp_path, game=game, exe=exe, modo=modo, ini_dir=ini_dir)


@pytest.mark.parametrize("modo", ["sse", "tes5vr"])
def test_ini_dir_declarado_pero_inexistente_falla_cerrado(
    tmp_path: pathlib.Path,
    modo: Literal["sse", "tes5vr"],
) -> None:
    """Una carpeta declarada que no existe tampoco satisface la propiedad.

    El resolver de #597 ya la rechaza en el camino productivo; acá se fija que la
    config —que es la dueña de la decisión— tampoco la acepte, para que un caller
    directo (rig, test, preview) no construya una corrida condenada.
    """
    game, exe = _arbol_minimo(tmp_path, nombre_juego=_NOMBRE_DE_JUEGO_POR_MODO[modo])

    with pytest.raises(DynDOLODValidationError, match="Skyrim.ini"):
        _config(tmp_path, game=game, exe=exe, modo=modo, ini_dir=tmp_path / "My Games" / "no-existe")


@pytest.mark.parametrize("modo", ["sse", "tes5vr"])
def test_ini_dir_no_declarado_conserva_el_comportamiento_vigente(
    tmp_path: pathlib.Path,
    modo: Literal["sse", "tes5vr"],
) -> None:
    """``ini_dir is None`` NO es una declaración inválida: es ausencia de ``-m:``.

    #601 valida una declaración EXPLÍCITA incompatible; no convierte ``-m:`` en
    obligatorio. Sin ``DYNDLOD_INI_DIR`` no hay carpeta que exigir, la config
    existe y el argv sale sin ``-m:`` — la herramienta resuelve su default por
    registro, comportamiento vigente desde #597.
    """
    game, exe = _arbol_minimo(tmp_path, nombre_juego=_NOMBRE_DE_JUEGO_POR_MODO[modo])

    config = _config(tmp_path, game=game, exe=exe, modo=modo, ini_dir=None)
    runner = DynDOLODRunner(config, readiness=ReadinessMode.DISABLED_FOR_TEST)

    argv = runner._build_xedit_args(None, herramienta=HerramientaDynDOLOD.DYNDOLOD)
    assert not [a for a in argv if a.startswith("-m:")], f"sin declaración no hay -m:: {argv}"


@symlink_guard
@pytest.mark.parametrize("modo", ["sse", "tes5vr"])
def test_un_enlace_al_archivo_esperado_se_acepta(
    tmp_path: pathlib.Path,
    modo: Literal["sse", "tes5vr"],
) -> None:
    """Política de enlaces, decidida y anclada: ``is_file()`` sigue el enlace.

    Es la misma semántica que el binario (``FileExists`` + ``TMemIniFile`` siguen
    el enlace) y la del resolver de #597 con la CARPETA declarada (la canonicaliza
    y no prohíbe enlaces). Sky-Claw no lee ni escribe esa INI: sólo compone la ruta
    que viaja al argv, así que rechazar el enlace sería más estricto que la
    herramienta sin ganancia de seguridad — y agregar contención por-archivo sería
    un framework nuevo que el threat model del repo no pide (FASE 9 de #601). Si
    algún día se decide lo contrario, este test es el que debe cambiar.
    """
    game, exe = _arbol_minimo(tmp_path, nombre_juego=_NOMBRE_DE_JUEGO_POR_MODO[modo])
    real = tmp_path / "otra-ubicacion" / "Skyrim.ini"
    real.parent.mkdir(parents=True, exist_ok=True)
    real.write_text("[General]\n", encoding="utf-8")
    ini_dir = _ini_dir_con(tmp_path, nombre=_NOMBRE_DE_JUEGO_POR_MODO[modo], contenido=())
    (ini_dir / "Skyrim.ini").symlink_to(real)

    config = _config(tmp_path, game=game, exe=exe, modo=modo, ini_dir=ini_dir)

    assert config.ini_dir == ini_dir

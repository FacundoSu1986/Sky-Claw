"""Modelo de SALIDA de los rituales — U-01 parte (2).

**Test-ancla de una CLASE de defecto, no de un caso.** U-01 parte (1) (#381)
cerró la ENTRADA: el sensor de visibilidad detecta el modlist invisible cuando
Sky-Claw corre standalone. Este archivo cierra la otra mitad, la que la auditoría
nombra como *"incoherencia de modelo: la SALIDA se asume redirigida a
``overwrite`` (USVFS) pero la ENTRADA se asume materializada"*.

La premisa *"el destino de salida es dependiente del entorno"* estaba escrita en
seis lugares del árbol y es **falsa en todos**: la redirección USVFS exige
heredar la VFS, y ningún tool spawneado por ``run_capture`` la hereda. En tres de
esos seis la premisa era el motivo declarado de que NO hubiera rollback
(``target_files=[]`` / ``snapshots=[]``), así que no era prosa muerta.

Se enumera la familia (patrón ``RITUAL_TOOL_MAP`` de
``tests/test_ritual_dispatch.py``) en vez de escribir un caso por servicio: un
servicio nuevo que resuelva su salida rompe el guard de completitud hasta que
alguien lo clasifique. Ver ``sky_claw/local/tools/output_targets`` para el
invariante.
"""

from __future__ import annotations

import pathlib
from unittest.mock import MagicMock, patch

from sky_claw.local.tools.dyndolod_runner import DynDOLODConfig, DynDOLODRunner
from sky_claw.local.tools.dyndolod_service import DynDOLODPipelineService
from sky_claw.local.tools.output_targets import (
    bashed_patch_target,
    bodyslide_output_target,
    dyndolod_output_target,
    pandora_output_target,
    synthesis_output_target,
)
from sky_claw.local.tools.pandora_service import PandoraPipelineService
from sky_claw.local.tools.synthesis_service import SynthesisPipelineService
from sky_claw.local.tools.wrye_bash_service import WryeBashPipelineService

_TOOLS_DIR = pathlib.Path(__file__).resolve().parent.parent / "sky_claw" / "local" / "tools"

#: Servicios que resuelven un destino de escritura y DEBEN hacerlo vía
#: ``output_targets`` (fuente única del invariante de deployment).
SERVICIOS_CON_SALIDA: frozenset[str] = frozenset(
    {
        "wrye_bash_service",
        "pandora_service",
        "synthesis_service",
        "dyndolod_service",
    }
)

#: Excluidos **con motivo explícito** (la regla de "Hermanos" del template pide
#: nombrarlos, no darlos por descartados de memoria).
SERVICIOS_SIN_SALIDA: dict[str, str] = {
    "loot_service": (
        "No produce artefacto nuevo: reordena el plugins.txt del perfil, que es "
        "su propia ENTRADA. Además es el ÚNICO que puede correr bajo la USVFS "
        "(vía el broker de brokered_loot), y ese lado ya lo decide su predicado "
        "_routes_through_physical_data — cablearlo acá duplicaría la detección."
    ),
    "xedit_service": (
        "QuickAutoClean reescribe el plugin IN-PLACE sobre su ruta de entrada; "
        "no hay un destino de salida distinto que resolver. execute_patch tampoco "
        "declara uno."
    ),
}


def _modulos_que_resuelven_salida() -> set[str]:
    """Servicios que declaran dónde aterriza una escritura — la familia a clasificar.

    Se detecta por el uso de ``WritePermissionsChecker``: sondear permisos es,
    literalmente, afirmar "acá es donde voy a escribir".
    """
    return {
        ruta.stem
        for ruta in _TOOLS_DIR.glob("*_service.py")
        if "WritePermissionsChecker" in ruta.read_text(encoding="utf-8")
    }


def test_la_enumeracion_cubre_toda_la_familia() -> None:
    """Guard de completitud: un servicio nuevo que declare un destino de escritura
    obliga a decidir si lo resuelve por el invariante, en vez de heredar en
    silencio la premisa del ``overwrite`` que este PR desmonta."""
    clasificados = SERVICIOS_CON_SALIDA | set(SERVICIOS_SIN_SALIDA)

    assert _modulos_que_resuelven_salida() == clasificados


# ---------------------------------------------------------------------------
# DynDOLOD — salida determinista bajo la raíz administrada (-o:)
# ---------------------------------------------------------------------------


def _resolver(*, game: pathlib.Path | None = None, mo2: pathlib.Path | None = None) -> MagicMock:
    resolver = MagicMock()
    resolver.get_skyrim_path.return_value = game
    resolver.get_mo2_path.return_value = mo2
    resolver.get_skyrim_path_raw.return_value = str(game) if game is not None else None
    resolver.get_mo2_path_raw.return_value = str(mo2) if mo2 is not None else None
    return resolver


def test_bashed_patch_aterriza_en_el_data_del_juego(tmp_path: pathlib.Path) -> None:
    """``bash.py`` corre con ``cwd=game_path`` y sin ruta de salida en el comando:
    el plugin cae en el ``Data`` físico. Es lo que ``_bashed_patch_target`` ya
    afirmaba para el manifiesto — acá pasa a ser la única fuente."""
    game = tmp_path / "Skyrim Special Edition"

    destino = bashed_patch_target(game)

    assert destino == game / "Data" / "Bashed Patch, 0.esp"


def test_wrye_bash_no_sondea_el_overwrite(tmp_path: pathlib.Path) -> None:
    """El ``overwrite`` solo se alcanza por redirección USVFS, y Wrye Bash no
    hereda la VFS (se spawnea directo por ``run_capture``). Sondearlo modelaba un
    modo de lanzamiento inexistente — y ese mismo modelo era el argumento para no
    snapshotear la salida (U-04)."""
    game = tmp_path / "game"
    mo2 = tmp_path / "mo2"
    svc = WryeBashPipelineService(
        lock_manager=MagicMock(),
        snapshot_manager=MagicMock(),
        journal=MagicMock(),
        path_resolver=_resolver(game=game, mo2=mo2),
    )

    targets = svc._permission_targets()

    assert mo2 / "overwrite" not in targets
    assert game / "Data" in targets


# ---------------------------------------------------------------------------
# Pandora — un único destino administrado
# ---------------------------------------------------------------------------


def test_pandora_tiene_un_unico_destino_administrado(tmp_path: pathlib.Path) -> None:
    game = tmp_path / "segmento" / ".." / "game"

    assert pandora_output_target(game=game) == game.resolve() / "Pandora_Output"
    assert pandora_output_target(game=None) is None


def test_pandora_service_sondea_solo_el_destino_administrado(tmp_path: pathlib.Path) -> None:
    """El servicio consume el destino singular; no conserva candidatas legacy."""
    game = tmp_path / "game"
    mo2 = tmp_path / "mo2"
    exe = tmp_path / "pandora" / "pandora.exe"
    output = pandora_output_target(game=game)
    assert output is not None
    output.mkdir(parents=True)
    resolver = _resolver(game=game, mo2=mo2)
    resolver.get_pandora_exe.return_value = exe
    svc = PandoraPipelineService(
        lock_manager=MagicMock(),
        snapshot_manager=MagicMock(),
        journal=MagicMock(),
        path_resolver=resolver,
    )

    targets = svc._permission_targets()

    assert targets == [output]


# ---------------------------------------------------------------------------
# DynDOLOD — el subproceso HEREDA el cwd, y los dos hermanos deben coincidir
# ---------------------------------------------------------------------------


def test_dyndolod_salida_determinista_bajo_la_raiz(tmp_path: pathlib.Path) -> None:
    """Con ``-o:`` la salida es determinista: 2 candidatos bajo la raíz administrada.

    Interpretación A (primaria): la herramienta crea su carpeta
    (``DynDOLOD_Output``/``TexGen_Output``) DENTRO del ``-o:`` — el layout por
    default lo confirma. Interpretación B (fallback acotado a la raíz): escribe
    directo. El cwd / dir del exe / raíz MO2 ya NO son raíces de staging: el
    subproceso recibe ``-o:`` y escribe solo en la raíz administrada.
    """
    mo2 = tmp_path / "mo2"
    exe_dir = tmp_path / "dyndolod"
    mo2.mkdir()
    exe_dir.mkdir()
    game = tmp_path / "game"
    game.mkdir()
    exe = exe_dir / "DynDOLODx64.exe"
    exe.write_text("", encoding="utf-8")

    runner = DynDOLODRunner(
        DynDOLODConfig(
            game_path=game,
            mo2_path=mo2,
            mo2_mods_path=mo2 / "mods",
            dyndolod_exe=exe,
        )
    )
    root = runner._config.output_root
    assert root == game.resolve() / "Sky-Claw" / "DynDOLOD"

    # Sin nada en disco: los stagings se resuelven como candidatos bajo la raíz.
    assert runner._find_dyndolod_output() == root / DynDOLODRunner.DYNDOLLOD_OUTPUT_NAME
    assert runner._find_texgen_output() == root / DynDOLODRunner.TEXGEN_OUTPUT_NAME

    # Interpretación A: la herramienta crea la subcarpeta dentro del -o:.
    staging = root / DynDOLODRunner.DYNDOLLOD_OUTPUT_NAME
    staging.mkdir(parents=True)
    (staging / "DynDOLOD.esp").write_text("salida real", encoding="utf-8")
    assert runner._find_dyndolod_output() == staging

    # Interpretación B: escribe directo en la raíz (fallback acotado).
    (staging / "DynDOLOD.esp").unlink()
    staging.rmdir()
    (root / "DynDOLOD.esp").write_text("directo", encoding="utf-8")
    assert runner._find_dyndolod_output() == root


def test_dyndolod_preflight_sondea_los_mismos_candidatos_que_el_runner(
    tmp_path: pathlib.Path,
) -> None:
    """Los dos hermanos: el sondeo de permisos y la búsqueda de salida miran la
    MISMA raíz administrada (la propiedad que el ancla viejo fijaba con el cwd).

    La parte que ancla la CLASE de defecto: sacar la raíz de un solo lado —o
    agregarle una raíz nueva a uno solo— rompe acá antes de llegar a producción.
    """
    game = tmp_path / "game"
    game.mkdir()
    mo2 = tmp_path / "mo2"
    (mo2 / "mods").mkdir(parents=True)
    exe_dir = tmp_path / "dyndolod"
    exe_dir.mkdir()
    exe = exe_dir / "DynDOLODx64.exe"
    exe.write_text("", encoding="utf-8")

    resolver = _resolver(game=game, mo2=mo2)
    resolver.get_mo2_mods_path.return_value = mo2 / "mods"
    resolver.get_dyndolod_exe.return_value = exe

    svc = DynDOLODPipelineService(
        lock_manager=MagicMock(),
        snapshot_manager=MagicMock(),
        journal=MagicMock(),
        path_resolver=resolver,
        event_bus=MagicMock(),
    )
    runner = DynDOLODRunner(
        DynDOLODConfig(
            game_path=game,
            mo2_path=mo2,
            mo2_mods_path=mo2 / "mods",
            dyndolod_exe=exe,
        )
    )

    targets = svc._permission_targets()
    root = runner._config.output_root
    assert root is not None
    assert root in targets
    assert root / DynDOLODRunner.DYNDOLLOD_OUTPUT_NAME in targets
    assert root / DynDOLODRunner.TEXGEN_OUTPUT_NAME in targets

    # Para poder CREAR la raíz en el primer run hay que sondear un eslabón que
    # EXISTA: con la raíz anidada bajo Sky-Claw/, `root.parent` tampoco está en
    # disco todavía y el checker se lo saltearía, dejando mudo justo el sondeo
    # que este assert cubre.
    assert game in targets
    assert not root.parent.exists()

    # El dir del exe SÍ se sondea: no es staging de salida, pero es donde la
    # herramienta escribe su log —el que `_leer_log` lee para dar el veredicto—
    # y su INI. Con el tool en un árbol de solo lectura, omitirlo dejaba el
    # preflight verde y el post-check sin evidencia.
    assert exe_dir in targets
    assert exe_dir / "Logs" in targets

    # La raíz MO2 sigue fuera: con `-o:` dejó de ser raíz de staging.
    assert mo2 not in targets


def test_preflight_no_sondea_fuera_del_juego_si_el_game_path_no_existe(tmp_path: pathlib.Path) -> None:
    """Regresión (review adversarial #441): el ascenso corta en el dir del juego.

    ``_primer_ancestro_existente`` sube hasta el primer directorio que existe. Con
    un game path mal configurado o todavía no montado, eso llegaba hasta la raíz
    del volumen: el preflight sondeaba —y aprobaba— un directorio ajeno al juego,
    dando verde sobre una configuración inválida que recién fallaba después.
    """
    # Con ``..`` en la ruta A PROPÓSITO: `root` se construye sobre
    # `game.resolve()` y `Path.__eq__` es léxico, así que un tope crudo no era
    # ancestro literal de la raíz y el corte no disparaba nunca (review
    # adversarial #441). El repo configura rutas así en sus propios tests.
    game = tmp_path / "segmento" / ".." / "no-montado" / "Skyrim"  # no existe
    mo2 = tmp_path / "mo2"
    (mo2 / "mods").mkdir(parents=True)
    exe_dir = tmp_path / "dyndolod"
    exe_dir.mkdir()
    exe = exe_dir / "DynDOLODx64.exe"
    exe.write_text("", encoding="utf-8")

    resolver = _resolver(game=game, mo2=mo2)
    resolver.get_mo2_mods_path.return_value = mo2 / "mods"
    resolver.get_dyndolod_exe.return_value = exe

    svc = DynDOLODPipelineService(
        lock_manager=MagicMock(),
        snapshot_manager=MagicMock(),
        journal=MagicMock(),
        path_resolver=resolver,
        event_bus=MagicMock(),
    )

    targets = svc._permission_targets()
    assert tmp_path not in targets, "no se sondea por encima del directorio del juego"
    assert not any(t == pathlib.Path(t.anchor) for t in targets), "nunca la raíz del volumen"


def test_dyndolod_tiene_una_raiz_administrada_unica(tmp_path: pathlib.Path) -> None:
    """La salida de DynDOLOD/TexGen vive bajo UNA raíz administrada (patrón Pandora).

    Los binarios la reciben vía ``-o:`` (patrón (b): ruta explícita en el comando),
    así que el destino deja de depender del cwd/dir del exe/raíz MO2 del proceso.
    ``None`` sin juego, mismo contrato que ``pandora_output_target``.
    """
    game = tmp_path / "segmento" / ".." / "game"

    assert dyndolod_output_target(game=game) == game.resolve() / "Sky-Claw" / "DynDOLOD"
    assert dyndolod_output_target(game=None) is None


def test_dyndolod_staging_cuelga_de_la_raiz(tmp_path: pathlib.Path) -> None:
    """Los dos stagings de la herramienta viven bajo la raíz administrada.

    Ancla los nombres: la herramienta crea ``DynDOLOD_Output``/``TexGen_Output``
    DENTRO del valor de ``-o:`` (el layout por default —junto al exe— lo confirma).
    Si alguien renombra las constantes del runner o cambia la raíz, esto se rompe
    antes de llegar a producción.
    """
    game = tmp_path / "game"
    root = dyndolod_output_target(game=game)
    assert root is not None

    assert root / DynDOLODRunner.DYNDOLLOD_OUTPUT_NAME == game.resolve() / "Sky-Claw" / "DynDOLOD" / "DynDOLOD_Output"
    assert root / DynDOLODRunner.TEXGEN_OUTPUT_NAME == game.resolve() / "Sky-Claw" / "DynDOLOD" / "TexGen_Output"


# ---------------------------------------------------------------------------
# Synthesis — un solo cálculo, no dos copias
# ---------------------------------------------------------------------------


def _synthesis(mo2: pathlib.Path, game: pathlib.Path) -> SynthesisPipelineService:
    return SynthesisPipelineService(
        lock_manager=MagicMock(),
        snapshot_manager=MagicMock(),
        journal=MagicMock(),
        path_resolver=_resolver(game=game, mo2=mo2),
        event_bus=MagicMock(),
    )


def _target_de_permisos(svc: SynthesisPipelineService) -> pathlib.Path:
    """Ruta que el preflight declara como destino (la del closure ``_permissions``)."""
    checker = MagicMock()
    with patch("sky_claw.local.validators.write_permissions.WritePermissionsChecker", checker):
        preflight = svc._ensure_preflight()
        assert preflight is not None
        preflight._permissions_check()  # type: ignore[misc]
    targets = checker.call_args.kwargs["targets"]
    assert len(targets) == 1
    return pathlib.Path(targets[0])


def test_synthesis_resuelve_la_salida_en_un_solo_lugar(tmp_path: pathlib.Path) -> None:
    """El destino del runner y el que el preflight sondea salen del MISMO
    resolver, en las DOS ramas (``overwrite`` presente y ausente).

    Estaban duplicados en dos métodos con un comentario "mismo cálculo que…" —
    la forma exacta del episodio #373 (hermanos en el mismo archivo). Con las dos
    ramas ancladas, cambiar una copia y no la otra rompe este test.
    """
    game = tmp_path / "game"
    exe = tmp_path / "Synthesis.exe"
    exe.write_text("", encoding="utf-8")

    # Rama A: sin overwrite en disco → mods/"Synthesis Output".
    mo2_sin = tmp_path / "mo2_sin"
    mo2_sin.mkdir()
    svc = _synthesis(mo2_sin, game)
    svc._path_resolver.get_synthesis_exe.return_value = exe
    esperado_a = synthesis_output_target(mo2=mo2_sin, override=None)
    assert esperado_a == mo2_sin / "mods" / "Synthesis Output"
    assert svc._ensure_synthesis_runner()._config.output_path == esperado_a
    assert _target_de_permisos(svc) == esperado_a

    # Rama B: con overwrite en disco → el overwrite (ruta EXPLÍCITA en el
    # comando, no redirección USVFS: por eso sigue siendo válido standalone).
    mo2_con = tmp_path / "mo2_con"
    (mo2_con / "overwrite").mkdir(parents=True)
    svc_b = _synthesis(mo2_con, game)
    svc_b._path_resolver.get_synthesis_exe.return_value = exe
    esperado_b = synthesis_output_target(mo2=mo2_con, override=None)
    assert esperado_b == mo2_con / "overwrite"
    assert svc_b._ensure_synthesis_runner()._config.output_path == esperado_b
    assert _target_de_permisos(svc_b) == esperado_b


def test_synthesis_respeta_el_override_del_sandbox(tmp_path: pathlib.Path) -> None:
    """El override de T-27b (``SandboxClone.overwrite_copy``) manda sobre ambas
    ramas: mover el cálculo al resolver no puede romper el aislamiento del
    sandbox."""
    mo2 = tmp_path / "mo2"
    (mo2 / "overwrite").mkdir(parents=True)
    clon = tmp_path / "sandbox" / "overwrite"

    assert synthesis_output_target(mo2=mo2, override=clon) == clon


# ---------------------------------------------------------------------------
# BodySlide — un subárbol propio POR GRUPO (U-04)
# ---------------------------------------------------------------------------


def test_bodyslide_tiene_un_destino_propio_por_grupo(tmp_path: pathlib.Path) -> None:
    game = tmp_path / "segmento" / ".." / "game"

    assert bodyslide_output_target(game=game, group="CBBE") == game.resolve() / "BodySlide_Output" / "CBBE"
    assert bodyslide_output_target(game=None, group="CBBE") is None


def test_bodyslide_rechaza_group_invalido_en_vez_de_confiar_en_el_llamador(tmp_path: pathlib.Path) -> None:
    """``group`` llega ya validado por ``BodySlideBatchParams`` en el único
    llamador de producción (CodeRabbit, PR #430), pero esta función es pública
    y su contrato de tipo es sólo ``str`` — nada impide un llamador futuro (o un
    test) que la invoque directo. Defensa en profundidad: rechaza "."/".." como
    valor completo y separadores de ruta ANTES de construir el target, con el
    mismo comportamiento de error que ya usa esta función (``None``)."""
    game = tmp_path / "game"

    assert bodyslide_output_target(game=game, group=".") is None
    assert bodyslide_output_target(game=game, group="..") is None
    assert bodyslide_output_target(game=game, group="a/b") is None
    assert bodyslide_output_target(game=game, group="a\\b") is None


def test_bodyslide_grupos_distintos_no_comparten_directorio(tmp_path: pathlib.Path) -> None:
    """Un rebuild de un grupo no puede pisar lo que ya existía de otro: cada
    grupo obtiene su propio subárbol, la misma propiedad que ``DirectoryRollback``
    exige (\"la herramienta regenera el target por completo\"). Un directorio
    compartido entre grupos violaría esa propiedad — un rebuild de ``CBBE``
    borraría lo que había de ``3BA``."""
    game = tmp_path / "game"

    cbbe = bodyslide_output_target(game=game, group="CBBE")
    tresa = bodyslide_output_target(game=game, group="3BA")

    assert cbbe is not None
    assert tresa is not None
    assert cbbe != tresa
    assert cbbe.parent == tresa.parent == game.resolve() / "BodySlide_Output"

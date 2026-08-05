from __future__ import annotations

import pathlib
from unittest.mock import AsyncMock, patch

import pytest

from sky_claw.local.tools.pandora_runner import (
    NOMBRE_DEL_LOG_DE_PANDORA,
    PandoraConfig,
    PandoraExecutionError,
    PandoraRunner,
)


def _preparar(tmp_path: pathlib.Path) -> tuple[PandoraRunner, pathlib.Path]:
    """Runner con el exe en su propio directorio y la ruta de su ``Engine.log``."""
    game = tmp_path / "Skyrim"
    game.mkdir(parents=True, exist_ok=True)
    dir_exe = tmp_path / "Pandora"
    dir_exe.mkdir(parents=True, exist_ok=True)
    runner = PandoraRunner(PandoraConfig(pandora_exe=dir_exe / "Pandora Behaviour Engine+.exe", game_path=game))
    return runner, dir_exe / NOMBRE_DEL_LOG_DE_PANDORA


async def _correr(runner: PandoraRunner, *, escribe: str = "", return_code: int = 0, log: pathlib.Path | None = None):
    """Ejecuta el runner simulando que Pandora APENDEA *escribe* a su log."""

    async def _fake(*_args: object, **_kwargs: object) -> tuple[bytes, bytes, int]:
        if log is not None and escribe:
            with log.open("a", encoding="utf-8") as handle:
                handle.write(escribe)
        return b"", b"", return_code

    with patch("sky_claw.local.tools.pandora_runner.run_capture", AsyncMock(side_effect=_fake)):
        return await runner.run_pandora()


async def test_pandora_pasa_una_sola_salida_absoluta_y_preserva_espacios(
    tmp_path: pathlib.Path,
) -> None:
    game = tmp_path / "Skyrim con espacios"
    exe = tmp_path / "Pandora con espacios" / "Pandora Behaviour Engine+.exe"
    runner = PandoraRunner(PandoraConfig(pandora_exe=exe, game_path=game))
    run_capture = AsyncMock(return_value=(b"ok", b"", 0))

    with patch("sky_claw.local.tools.pandora_runner.run_capture", run_capture):
        resultado = await runner.run_pandora()

    assert resultado.success is True
    argv = run_capture.await_args.args[0]
    assert argv.count("--output") == 1
    output = pathlib.Path(argv[argv.index("--output") + 1])
    assert output == game.resolve() / "Pandora_Output"
    assert output.is_absolute()
    assert run_capture.await_args.kwargs["cwd"] == str(game.resolve())


def test_output_path_del_runner_es_el_destino_administrado(tmp_path: pathlib.Path) -> None:
    game = tmp_path / "segmento" / ".." / "game"
    exe = tmp_path / "Pandora.exe"
    runner = PandoraRunner(PandoraConfig(pandora_exe=exe, game_path=game))

    assert runner.output_path == game.resolve() / "Pandora_Output"


def test_output_path_falla_con_error_de_dominio_si_falta_game_path(tmp_path: pathlib.Path) -> None:
    config = PandoraConfig(pandora_exe=tmp_path / "Pandora.exe", game_path=tmp_path)
    object.__setattr__(config, "game_path", None)
    runner = PandoraRunner(config)

    with pytest.raises(PandoraExecutionError, match="game_path.*Pandora_Output"):
        _ = runner.output_path


# --------------------------------------------------------------------------- #
# El veredicto NO sale del exit code: Pandora es error-tolerant
#
# El motor revierte el nodo inválido y sigue generando el resto (README de
# Monitor221hz/Pandora-Behaviour-Engine-Plus). O sea que termina en 0 habiendo
# descartado mods de animación, y el SOP (`sky_claw/local/AGENTS.md` §2.4) dice
# que eso es justamente lo que produce T-poses y combat anims rotas. La única
# señal de qué se aplicó es `Engine.log`.
#
# Mismo mecanismo que `bodyslide_runner._leer_log` (#433), con una diferencia
# deliberada: BodySlide acota la corrida por una firma de inicio documentada en
# su fuente (`Log::Initialize`), y para Pandora NO hay una firma verificada. Acá
# la atribución se hace por OFFSET —tamaño del log antes de spawnear— que no
# depende de ningún formato y por lo tanto no puede envejecer mal.
# --------------------------------------------------------------------------- #


async def test_exit_cero_con_error_en_el_log_no_es_exito(tmp_path: pathlib.Path) -> None:
    """El caso que este mecanismo existe para cerrar: falso verde por exit code."""
    runner, log = _preparar(tmp_path)

    resultado = await _correr(
        runner,
        escribe="INFO: Assembler > x > parse > y > ok\nERROR: Assembler > mod.xml > parse > nodo > falló\n",
        log=log,
    )

    assert resultado.success is False
    assert resultado.return_code == 0
    assert "ERROR" in resultado.message
    assert "mod.xml" in resultado.message


async def test_exit_cero_con_fatal_en_el_log_no_es_exito(tmp_path: pathlib.Path) -> None:
    runner, log = _preparar(tmp_path)

    resultado = await _correr(runner, escribe="FATAL: Dispatcher > x > export > y > murió\n", log=log)

    assert resultado.success is False
    assert "FATAL" in resultado.message


async def test_exit_cero_con_log_limpio_es_exito_y_message_vacio(tmp_path: pathlib.Path) -> None:
    """Contrato ``success``/``message``: en éxito el message queda vacío."""
    runner, log = _preparar(tmp_path)

    resultado = await _correr(runner, escribe="INFO: Assembler > x > parse > y > ok\n", log=log)

    assert resultado.success is True
    assert resultado.message == ""


async def test_warn_se_reporta_pero_no_decide(tmp_path: pathlib.Path) -> None:
    """``WARN`` es "unexpected condition", no "prevented work" (README).

    Hacerlo fallar convertiría advertencias rutinarias en falsos ROJOS, que es
    tan malo como el falso verde que este mecanismo cierra. Se cuenta y se
    surface, no decide.
    """
    runner, log = _preparar(tmp_path)

    resultado = await _correr(runner, escribe="WARN: Validator > x > check > y > revertido\n", log=log)

    assert resultado.success is True
    assert any("revertido" in w for w in resultado.warnings)


async def test_un_error_de_una_corrida_anterior_no_ensucia_esta(tmp_path: pathlib.Path) -> None:
    """La prueba de la atribución por offset, y el falso rojo más probable.

    `Engine.log` se acumula entre corridas. Sin acotar por el tamaño previo, el
    `ERROR` de la corrida de ayer haría fallar para siempre todas las siguientes.
    """
    runner, log = _preparar(tmp_path)
    log.write_text("ERROR: Assembler > viejo.xml > parse > nodo > falló\n", encoding="utf-8")

    resultado = await _correr(runner, escribe="INFO: Assembler > x > parse > y > ok\n", log=log)

    assert resultado.success is True
    assert "viejo.xml" not in resultado.message


async def test_log_ausente_no_cambia_el_veredicto(tmp_path: pathlib.Path) -> None:
    """Sin log no hay señal: se conserva el comportamiento por exit code.

    Best-effort a propósito — la ubicación de `Engine.log` no está verificada
    contra un rig real, así que una ruta equivocada degrada al comportamiento de
    hoy y nunca a un falso rojo.
    """
    runner, _log = _preparar(tmp_path)

    resultado = await _correr(runner)

    assert resultado.success is True
    assert resultado.message == ""


async def test_log_truncado_durante_la_corrida_no_decide(tmp_path: pathlib.Path) -> None:
    """Si el log se rotó, el texto no es atribuible: se muestra pero no decide."""
    runner, log = _preparar(tmp_path)
    log.write_text("x" * 5000, encoding="utf-8")

    async def _fake(*_args: object, **_kwargs: object) -> tuple[bytes, bytes, int]:
        # Pandora rota el log: queda MÁS CHICO que la marca previa.
        log.write_text("ERROR: Assembler > a > b > c > d\n", encoding="utf-8")
        return b"", b"", 0

    with patch("sky_claw.local.tools.pandora_runner.run_capture", AsyncMock(side_effect=_fake)):
        resultado = await runner.run_pandora()

    assert resultado.success is True, "texto no atribuible no debe producir un falso rojo"


async def test_exit_non_zero_sigue_fallando_aunque_el_log_este_limpio(tmp_path: pathlib.Path) -> None:
    """El log AGREGA una condición de fallo; no relaja la que ya había."""
    runner, log = _preparar(tmp_path)

    resultado = await _correr(runner, escribe="INFO: todo bien\n", return_code=3, log=log)

    assert resultado.success is False
    assert "3" in resultado.message


async def test_exit_non_zero_adjunta_la_cola_cruda_si_no_reconocio_severidades(
    tmp_path: pathlib.Path,
) -> None:
    """La red bajo la suposición del formato de log.

    El patrón de severidad no está verificado contra un log real. Si el formato
    fuera otro, ``fallas`` queda vacío; sin adjuntar la cola cruda el operador se
    queda con un exit code pelado y ninguna pista de por qué falló.
    """
    runner, log = _preparar(tmp_path)

    resultado = await _correr(runner, escribe="algo salió mal en un formato inesperado\n", return_code=2, log=log)

    assert resultado.success is False
    assert "formato inesperado" in resultado.message


async def test_reconoce_la_severidad_con_y_sin_corchetes(tmp_path: pathlib.Path) -> None:
    """El README documenta la plantilla ``[Severity]: ...`` con los corchetes como
    marcador de campo. No está verificado contra un log real si la línea sale
    ``ERROR:`` o ``[ERROR]:``, así que se aceptan las dos formas."""
    runner, log = _preparar(tmp_path)

    resultado = await _correr(runner, escribe="[ERROR]: Assembler > a > b > c > d\n", log=log)

    assert resultado.success is False

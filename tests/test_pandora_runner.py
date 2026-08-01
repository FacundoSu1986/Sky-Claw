from __future__ import annotations

import pathlib
from unittest.mock import AsyncMock, patch

import pytest

from sky_claw.local.tools.pandora_runner import PandoraConfig, PandoraExecutionError, PandoraRunner


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

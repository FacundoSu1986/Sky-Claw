"""#665 — el ``--help`` no puede declarar un default distinto del que corre.

El síntoma original: ``--mode`` decía ``(default: cli)`` también en el ``.exe``
congelado, donde el default efectivo es ``gui``. El hermano en el mismo parser:
``--provider`` decía ``(default: deepseek)`` aunque el default efectivo sale de
``config.llm_provider`` (p. ej. ``anthropic`` si el usuario lo configuró).

La propiedad se ancla sobre el CONJUNTO, no sobre el caso roto:

* para TODO argumento cuyo help declara ``(default: X)``, ``X`` es exactamente el
  valor que ``parse_args([])`` produce para ese ``dest``;
* en TODOS los contextos que mueven defaults: ``sys.frozen`` ausente/presente y
  config sin/con ``llm_provider`` propio.

El conjunto de argumentos que declaran default se congela por igualdad literal:
agregar un ``(default: ...)`` nuevo rompe el ancla hasta que se revisa, y el test
de comportamiento lo cubre automáticamente porque recorre ``parser._actions``.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import pathlib
import re
import sys

import pytest

from sky_claw import __main__ as cli
from sky_claw.config import Config

#: ``(default: X)`` tal como lo ve el usuario, ya expandido por argparse.
_PATRON_DEFAULT_DECLARADO = re.compile(r"\(default: ([^)]*)\)")

#: Argumentos que hoy declaran su default en el help (por ``dest``).
DESTS_CON_DEFAULT_DECLARADO = frozenset(
    {
        "mode",
        "provider",
        "vfs_profile",
        "vfs_timeout",
        "webhook_host",
        "webhook_port",
    }
)

#: Contextos que cambian defaults: (congelado, llm_provider en config.toml).
CONTEXTOS = [
    pytest.param((False, None), id="fuente-config-vacia"),
    pytest.param((True, None), id="congelado-config-vacia"),
    pytest.param((False, "anthropic"), id="fuente-config-anthropic"),
    pytest.param((True, "anthropic"), id="congelado-config-anthropic"),
]


@pytest.fixture()
def contexto_cli(
    request: pytest.FixtureRequest,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> argparse.ArgumentParser:
    """Arma el parser real bajo el contexto (congelado, provider) parametrizado."""
    congelado, provider = request.param
    config_path = tmp_path / "config.toml"
    if provider is not None:
        config_path.write_text(f'llm_provider = "{provider}"\n', encoding="utf-8")
    monkeypatch.setattr(Config, "DEFAULT_CONFIG_FILE", config_path)
    monkeypatch.setattr(Config, "DEFAULT_CONFIG_DIR", tmp_path)
    if congelado:
        monkeypatch.setattr(sys, "frozen", True, raising=False)
    else:
        monkeypatch.delattr(sys, "frozen", raising=False)
    # Sin wrap: cada help queda en una sola línea del --help renderizado.
    monkeypatch.setenv("COLUMNS", "400")
    return cli._build_parser()


def _help_expandido(parser: argparse.ArgumentParser, action: argparse.Action) -> str:
    """Help de UNA acción tal como lo imprime argparse (``%(default)s`` resuelto)."""
    if not action.help or action.help == argparse.SUPPRESS:
        return ""
    return parser._get_formatter()._expand_help(action)


def _defaults_declarados(parser: argparse.ArgumentParser) -> dict[str, str]:
    declarados: dict[str, str] = {}
    for action in parser._actions:
        coincidencia = _PATRON_DEFAULT_DECLARADO.search(_help_expandido(parser, action))
        if coincidencia:
            declarados[action.dest] = coincidencia.group(1)
    return declarados


@pytest.mark.parametrize("contexto_cli", CONTEXTOS, indirect=True)
def test_cada_default_declarado_coincide_con_el_efectivo(contexto_cli: argparse.ArgumentParser) -> None:
    # Arrange
    parser = contexto_cli
    efectivos = vars(parser.parse_args([]))

    # Act
    declarados = _defaults_declarados(parser)

    # Assert
    divergencias = {
        dest: {"declarado": texto, "efectivo": str(efectivos[dest])}
        for dest, texto in declarados.items()
        if texto != str(efectivos[dest])
    }
    assert not divergencias, f"El --help declara defaults que no son los que corren: {divergencias}"


@pytest.mark.parametrize("contexto_cli", CONTEXTOS, indirect=True)
def test_el_conjunto_de_defaults_declarados_esta_congelado(contexto_cli: argparse.ArgumentParser) -> None:
    # Arrange / Act
    declarados = set(_defaults_declarados(contexto_cli))

    # Assert — igualdad literal: un default declarado nuevo (o uno que
    # desaparece) obliga a revisar esta lista y su contrato.
    assert declarados == DESTS_CON_DEFAULT_DECLARADO


@pytest.mark.parametrize("contexto_cli", CONTEXTOS, indirect=True)
def test_ningun_help_menciona_default_fuera_del_formato_verificado(contexto_cli: argparse.ArgumentParser) -> None:
    """Un ``defaults to X`` o ``default X`` suelto escaparía al chequeo de arriba."""
    # Arrange
    parser = contexto_cli
    declarados = _defaults_declarados(parser)

    # Act
    sueltos = {
        action.dest: _help_expandido(parser, action)
        for action in parser._actions
        if action.dest not in declarados and re.search(r"\bdefaults?\b", _help_expandido(parser, action))
    }

    # Assert
    assert not sueltos, f"Help que menciona un default sin el formato '(default: X)': {sueltos}"


@pytest.mark.parametrize("contexto_cli", CONTEXTOS, indirect=True)
def test_el_help_renderizado_declara_el_modo_efectivo(contexto_cli: argparse.ArgumentParser) -> None:
    """Superficie del usuario final: lo que imprime ``--help`` de verdad."""
    # Arrange
    parser = contexto_cli
    modo_efectivo = parser.parse_args([]).mode
    salida = io.StringIO()

    # Act
    with contextlib.redirect_stdout(salida), pytest.raises(SystemExit):
        parser.parse_args(["--help"])

    # Assert
    lineas_modo = [linea for linea in salida.getvalue().splitlines() if "Operation mode" in linea]
    assert len(lineas_modo) == 1
    assert lineas_modo[0].rstrip().endswith(f"Operation mode (default: {modo_efectivo})")


@pytest.mark.parametrize(
    ("congelado", "modo_esperado"),
    [pytest.param(False, "cli", id="fuente"), pytest.param(True, "gui", id="congelado")],
)
def test_parse_args_conserva_el_default_de_modo_por_contexto(
    congelado: bool,
    modo_esperado: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """El fix alinea el texto; el comportamiento (cli/gui) no cambia."""
    # Arrange
    if congelado:
        monkeypatch.setattr(sys, "frozen", True, raising=False)
    else:
        monkeypatch.delattr(sys, "frozen", raising=False)

    # Act
    args = cli._parse_args([])

    # Assert
    assert args.mode == modo_esperado

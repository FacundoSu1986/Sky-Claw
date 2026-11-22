"""Ancla: todo entorno curado de un proceso hijo conserva ``USERNAME`` (requisito de USVFS 0.5.6.1).

Por qué existe: bajo MO2 2.5.2, ``usvfs_x64.dll`` 0.5.6.1 hace
``std::string(getenv("USERNAME"))`` sin chequear NULL en ``shm_sink::sink_it_`` al
inicializarse en CADA proceso enganchado. Si el padre lanza al hijo con un entorno
curado sin ``USERNAME``, el hijo muere con 0xC0000005 en ``usvfs_x64.dll+0xa6783``
antes de ejecutar una sola instrucción propia. Le pasó al CLI de Synthesis lanzado
por el worker USVFS (S5 real, 2026-11-22): el worker y el nieto vivían porque
heredaban el entorno de MO2, y solo el CLI —el único con entorno curado— caía.
``cmd.exe`` con el mismo entorno cae igual: no es .NET.

La propiedad es del mecanismo, no de un runner: *el aislamiento de secretos por
lista blanca solo es compatible con USVFS si TODAS las listas blancas llevan
``USERNAME``*. Por eso el test enumera por AST cada lista blanca de entorno OS del
paquete (las reconoce por contener ``SYSTEMROOT``), congela el censo por igualdad
literal y exige ``USERNAME`` en cada una. Una lista blanca nueva rompe el censo
hasta que se agrega acá, y al agregarla entra en la exigencia.

``USERNAME`` no es un secreto nuevo para el hijo: ``USERPROFILE`` (ya permitido)
contiene el nombre de la cuenta y cualquier proceso lo obtiene con ``GetUserNameW``.
"""

from __future__ import annotations

import ast
import importlib
import pathlib
from typing import Any
from unittest.mock import patch

import pytest

RAIZ = pathlib.Path(__file__).resolve().parents[1]
PAQUETE = RAIZ / "sky_claw"

#: Variable que identifica una lista blanca de entorno OS (toda lista útil para un hijo Windows la necesita).
MARCADOR_LISTA_OS = "SYSTEMROOT"

#: Censo congelado: (módulo, nombre) de cada lista blanca de entorno OS del paquete.
LISTAS_BLANCAS_ESPERADAS = {
    ("sky_claw/local/tools/synthesis_runner.py", "_SYNTHESIS_ALLOWED_OS_ENV"),
    ("sky_claw/local/tools/xedit_bridge_client.py", "VARIABLES_ENTORNO_PERMITIDAS"),
}


def _cadenas_literales(nodo: ast.AST) -> set[str] | None:
    """Cadenas de un literal set/list/tuple o de ``frozenset({...})``/``set([...])``; None si no aplica."""
    if isinstance(nodo, ast.Call) and isinstance(nodo.func, ast.Name) and nodo.func.id in {"frozenset", "set"}:
        if len(nodo.args) != 1:
            return None
        nodo = nodo.args[0]
    if not isinstance(nodo, (ast.Set, ast.List, ast.Tuple)):
        return None
    cadenas = {elt.value for elt in nodo.elts if isinstance(elt, ast.Constant) and isinstance(elt.value, str)}
    return cadenas or None


def _censar_listas_blancas() -> set[tuple[str, str]]:
    encontradas: set[tuple[str, str]] = set()
    for archivo in sorted(PAQUETE.rglob("*.py")):
        arbol = ast.parse(archivo.read_text(encoding="utf-8"), filename=str(archivo))
        for nodo in ast.walk(arbol):
            objetivos: list[ast.expr]
            if isinstance(nodo, ast.Assign):
                objetivos, valor = list(nodo.targets), nodo.value
            elif isinstance(nodo, ast.AnnAssign) and nodo.value is not None:
                objetivos, valor = [nodo.target], nodo.value
            else:
                continue
            cadenas = _cadenas_literales(valor)
            if not cadenas or MARCADOR_LISTA_OS not in {c.upper() for c in cadenas}:
                continue
            for objetivo in objetivos:
                nombre = objetivo.id if isinstance(objetivo, ast.Name) else ast.unparse(objetivo)
                encontradas.add((archivo.relative_to(RAIZ).as_posix(), nombre))
    return encontradas


def test_censo_de_listas_blancas_de_entorno_esta_congelado() -> None:
    assert _censar_listas_blancas() == LISTAS_BLANCAS_ESPERADAS


@pytest.mark.parametrize(("modulo", "nombre"), sorted(LISTAS_BLANCAS_ESPERADAS))
def test_cada_lista_blanca_conserva_username_para_usvfs(modulo: str, nombre: str) -> None:
    mod = importlib.import_module(modulo.removesuffix(".py").replace("/", "."))
    lista = getattr(mod, nombre)
    assert "USERNAME" in {clave.upper() for clave in lista}, (
        f"{modulo}:{nombre} filtra USERNAME; un hijo lanzado bajo USVFS 0.5.6.1 muere en usvfs_x64.dll+0xa6783"
    )


# --- Comportamiento: el valor llega al hijo por las dos superficies que hoy curan entorno ------------


def test_synthesis_runner_conserva_username_en_el_entorno(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.test_synthesis_pr1_hardening import _crear_entorno_base

    monkeypatch.setenv("USERNAME", "usuario-canario")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "secreto-que-no-debe-pasar")
    runner, _ = _crear_entorno_base(tmp_path)
    por_clave = {clave.upper(): valor for clave, valor in runner._build_subprocess_env().items()}
    assert por_clave.get("USERNAME") == "usuario-canario"
    assert "TELEGRAM_BOT_TOKEN" not in por_clave


async def test_synthesis_runner_lanza_el_cli_con_username(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """El entorno que llega a ``run_capture`` —el que ve el hijo enganchado— lleva USERNAME."""
    from tests.test_synthesis_pr1_hardening import _construir_tes4, _crear_entorno_base

    monkeypatch.setenv("USERNAME", "usuario-canario")
    runner, output = _crear_entorno_base(tmp_path)
    capturado: dict[str, Any] = {}

    async def _captura_falsa(args: list[str], **kwargs: Any) -> tuple[bytes, bytes, int]:
        capturado.update(kwargs)
        (output / "Synthesis.esp").write_bytes(_construir_tes4(["Skyrim.esm"]))
        return (
            b"[PatcherLight] Starting run.\n[PatcherLight] Run successful.\nExported patch to final destination: out\n",
            b"",
            0,
        )

    with patch("sky_claw.local.tools.synthesis_runner.run_capture", _captura_falsa):
        resultado = await runner.run_pipeline(["PatcherLight"])

    assert resultado.success is True
    assert {clave.upper(): valor for clave, valor in capturado["env"].items()}.get("USERNAME") == "usuario-canario"


def test_xedit_bridge_conserva_username_en_el_entorno(monkeypatch: pytest.MonkeyPatch) -> None:
    from sky_claw.local.tools.xedit_bridge_client import _construir_entorno

    monkeypatch.setenv("USERNAME", "usuario-canario")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "secreto-que-no-debe-pasar")
    por_clave = {clave.upper(): valor for clave, valor in _construir_entorno({}).items()}
    assert por_clave.get("USERNAME") == "usuario-canario"
    assert "TELEGRAM_BOT_TOKEN" not in por_clave

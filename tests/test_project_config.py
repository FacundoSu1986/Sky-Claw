"""P0.3 contract tests: pytest reproducibility and project config invariants.

These tests assert static properties of configuration files so CI catches
regressions in project setup without spawning subprocesses.
"""

from __future__ import annotations

import re
import tomllib
from importlib.metadata import version
from pathlib import Path

# `packaging` no se declara en [dev] porque es dependencia dura de pytest
# (`packaging>=20`): si no está, este archivo no puede ni colectarse.
import pytest
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet

REPO_ROOT = Path(__file__).parent.parent


# ---------------------------------------------------------------------------
# P0.3 — Pytest reproducible on Windows (no PermissionError on AppData\Temp)
# ---------------------------------------------------------------------------


def test_pytest_addopts_sets_basetemp() -> None:
    """pyproject.toml must set addopts with --basetemp=.pytest-tmp.

    RED path: [tool.pytest.ini_options] currently has no addopts key, so
    pytest uses AppData\\Local\\Temp\\pytest-of-User which raises PermissionError
    on Windows due to inherited ACLs from previous runs.
    """
    with (REPO_ROOT / "pyproject.toml").open("rb") as f:
        pyproject = tomllib.load(f)

    addopts: str = pyproject.get("tool", {}).get("pytest", {}).get("ini_options", {}).get("addopts", "")
    assert "--basetemp" in addopts, (
        "[tool.pytest.ini_options] must include addopts with '--basetemp=.pytest-tmp' "
        "to avoid PermissionError on Windows (AppData ACL residues). "
        f"Current addopts: {addopts!r}"
    )
    assert ".pytest-tmp" in addopts, (
        f"basetemp must point to '.pytest-tmp' (workspace-local, gitignored). Current addopts: {addopts!r}"
    )


def test_pytest_trata_warnings_de_lifecycle_como_errores() -> None:
    """El gate debe fallar ante fugas async sin endurecer warnings ajenos."""
    with (REPO_ROOT / "pyproject.toml").open("rb") as file:
        pyproject = tomllib.load(file)

    filterwarnings: list[str] = pyproject["tool"]["pytest"]["ini_options"].get("filterwarnings", [])
    assert set(filterwarnings) == {
        "error::RuntimeWarning",
        "error::ResourceWarning",
        "error::pytest.PytestUnraisableExceptionWarning",
    }


def _especificador_de_pytest_asyncio() -> str:
    """El requisito literal que ``[dev]`` declara para ``pytest-asyncio``."""
    with (REPO_ROOT / "pyproject.toml").open("rb") as file:
        pyproject = tomllib.load(file)

    dev: list[str] = pyproject["project"]["optional-dependencies"]["dev"]
    candidatos = [d for d in dev if d.replace(" ", "").startswith("pytest-asyncio")]

    # Aserción explícita en vez de `next(...)`: si la dependencia se renombra o
    # desaparece, un StopIteration deja un traceback sin decir qué propiedad se
    # rompió, y estas anclas existen justamente para nombrarla.
    assert len(candidatos) == 1, f"se esperaba exactamente un pytest-asyncio en [dev], hay {candidatos}"

    return candidatos[0]


def _piso_declarado_de_pytest_asyncio() -> tuple[tuple[int, int], str]:
    """``((mayor, menor), especificador)`` del piso que ``[dev]`` declara.

    Trunca a ``(mayor, menor)`` a propósito: su único consumidor pregunta si el
    piso bajó de 1.4, y eso se decide en esa granularidad. La comprobación de
    *satisfacción* del requisito NO usa esto — ver
    :func:`test_el_pytest_asyncio_instalado_respeta_el_piso_declarado`.
    """
    especificador = _especificador_de_pytest_asyncio()
    piso = re.search(r">=\s*(\d+)\.(\d+)", especificador)

    assert piso is not None, f"pytest-asyncio tiene que declarar un piso `>=X.Y`, dice: {especificador}"
    return (int(piso.group(1)), int(piso.group(2))), especificador


def test_pytest_asyncio_no_puede_bajar_del_piso_que_fuga_el_loop() -> None:
    """El piso de ``pytest-asyncio`` es la corrección real de la fuga de #414.

    Hasta 1.3.0 inclusive, ``_temporary_event_loop_policy`` abría con
    ``old_loop = _get_event_loop_no_warn()``, que llama a
    ``asyncio.get_event_loop()`` con el ``DeprecationWarning`` silenciado — y esa
    función **crea** un loop cuando la sesión no tiene uno actual. El huérfano
    nunca se corre ni se cierra: queda de "actual" hasta que alguien ponga el
    loop en ``None``, y ahí el GC lo finaliza con un ``unclosed event loop`` más
    los dos sockets de su self-pipe. Con ``error::ResourceWarning`` (#409), eso
    es la suite en rojo, en un test al azar.

    Se ancla el piso y no la ausencia del síntoma porque **bajar la versión es
    la única forma de traer el bug de vuelta**, y un rango sin piso lo permite en
    silencio: el ``>=1.0`` anterior resolvía a 1.3.0 vía ``uv.lock`` mientras CI
    —que instala con ``pip install -e ".[dev]"``, sin lock— tomaba 1.4.0. Esa
    divergencia es la razón de que #413 y #414 vieran un flake local que CI no
    reproducía.
    """
    piso, especificador = _piso_declarado_de_pytest_asyncio()

    assert piso >= (1, 4), f"el piso de pytest-asyncio no puede bajar de 1.4 (fuga de #414), dice: {especificador}"


def test_el_pytest_asyncio_instalado_respeta_el_piso_declarado() -> None:
    """El piso declarado no protege nada si el entorno quedó viejo.

    El ancla de arriba mira lo *declarado*; quien corre la suite es lo
    *instalado*. Un venv sin re-sincronizar sigue en 1.3.0 y reproduce la fuga
    entera de #415 — la suite en rojo en un test al azar, con la firma que ya
    costó dos PRs (#413, #414) de diagnóstico y tres víctimas inocentes
    distintas en tres corridas. Sin esta mitad, el síntoma vuelve a parecer un
    test de cancelación inestable en vez de un entorno desactualizado, que es
    exactamente el rodeo que hay que evitar.

    Se afirma contra el requisito declarado y no contra ``>=1.4`` literal para
    que subir el piso no deje esta mitad atrás: es el mismo par
    declarado/efectivo que ya mordió una vez.

    La comparación la hace ``packaging`` (PEP 440) y no un par
    ``(mayor, menor)`` propio: truncar dejaba pasar ``1.4.0rc1`` contra
    ``>=1.4`` —que PEP 440 considera *anterior*— y un ``1.4.0`` instalado contra
    un piso futuro ``>=1.4.1``. Un ancla que no puede afirmar lo que dice
    afirmar es peor que no tenerla (review Codex).
    """
    instalado = version("pytest-asyncio")
    especificador = _especificador_de_pytest_asyncio()
    requisito = Requirement(especificador)

    # prereleases=True para preguntar por el orden PEP 440 real y no por la
    # política de "ignorar prereleases": un 1.5.0rc1 SÍ satisface `>=1.4`.
    assert requisito.specifier.contains(instalado, prereleases=True), (
        f"el entorno tiene pytest-asyncio {instalado} y [dev] pide {especificador}. "
        "Hasta 1.3.0 la sesión fabrica y filtra un event loop, y el GC acusa a un "
        "test al azar (#415). Re-sincronizá el entorno: `uv sync --extra dev`."
    )


def test_el_comparador_de_versiones_no_trunca_el_patch_ni_las_prerelease() -> None:
    """Los dos agujeros que tenía comparar solo ``(mayor, menor)``.

    Sin esto, el ancla de arriba se apoya en una semántica que nadie verifica:
    es el mismo defecto —afirmar menos de lo que se declara— un nivel más
    abajo. Se enumeran los dos casos que un truncado a dos componentes resuelve
    mal, cada uno con su contraparte correcta, más el caso que motivó el ancla.
    """
    # Prerelease: PEP 440 la ordena ANTES de la final; truncar la daba por 1.4.
    assert not SpecifierSet(">=1.4").contains("1.4.0rc1", prereleases=True)
    assert SpecifierSet(">=1.4").contains("1.4.0", prereleases=True)
    # Patch: un piso futuro `>=1.4.1` no puede quedar satisfecho por 1.4.0.
    assert not SpecifierSet(">=1.4.1").contains("1.4.0", prereleases=True)
    assert SpecifierSet(">=1.4.1").contains("1.4.1", prereleases=True)
    # El caso que motivó todo: 1.3.0 nunca satisface el piso.
    assert not SpecifierSet(">=1.4").contains("1.3.0", prereleases=True)


def test_pytest_tmp_dir_is_gitignored() -> None:
    """.pytest-tmp/ must be listed in .gitignore to avoid committing temp artifacts.

    RED path: .gitignore currently has no entry for .pytest-tmp.
    """
    gitignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert re.search(r"^\.pytest-tmp", gitignore, re.MULTILINE), (
        ".gitignore must contain an entry matching '.pytest-tmp' so pytest basetemp artefacts are never committed."
    )


def test_conftest_has_session_finish_cleanup() -> None:
    """tests/conftest.py must define a pytest_sessionfinish hook for basetemp cleanup.

    RED path: conftest.py ends at line 84 with no pytest_sessionfinish hook,
    leaving .pytest-tmp with Windows ACL locks between runs.
    """
    conftest = (REPO_ROOT / "tests" / "conftest.py").read_text(encoding="utf-8")
    assert "pytest_sessionfinish" in conftest, (
        "tests/conftest.py must define a 'pytest_sessionfinish' hook "
        "that cleans up .pytest-tmp with ACL-tolerant shutil.rmtree "
        "(onerror handler) to prevent PermissionError on consecutive Windows runs."
    )


def test_locks_no_conservan_dependencias_del_stategraph_retirado() -> None:
    """Los dos caminos de instalación deben retirar la familia LangGraph."""
    dependencias_retiradas = {
        "langchain-core",
        "langgraph",
        "langgraph-checkpoint",
        "langgraph-sdk",
        "langsmith",
    }
    requirements_lock = (REPO_ROOT / "requirements.lock").read_text(encoding="utf-8")
    uv_lock = (REPO_ROOT / "uv.lock").read_text(encoding="utf-8")

    for dependencia in dependencias_retiradas:
        assert not re.search(rf"(?m)^{re.escape(dependencia)}==", requirements_lock), (
            f"requirements.lock todavía instala {dependencia}"
        )
        assert f'name = "{dependencia}"' not in uv_lock, f"uv.lock todavía instala {dependencia}"


def test_modulos_de_logging_tienen_override_mypy_strict() -> None:
    with (REPO_ROOT / "pyproject.toml").open("rb") as file:
        pyproject = tomllib.load(file)
    target_modules = {
        "sky_claw._logging_runtime",
        "sky_claw.logging_config",
        "sky_claw.__main__",
    }
    strict_modules: set[str] = set()
    for override in pyproject["tool"]["mypy"]["overrides"]:
        if override.get("strict") is True and override.get("ignore_errors") is False:
            strict_modules.update(override["module"])
        if override.get("ignore_errors") is True:
            assert target_modules.isdisjoint(override["module"])
    assert target_modules <= strict_modules


def test_seams_pr5_orchestrator_bajo_mypy_strict() -> None:
    """Los seams PR5 nacen bajo mypy estricto y no quedan silenciosamente exentos.

    Guardrail hermético para el P1 de #540: ``active_plugins`` y
    ``plugin_limit_guard`` viven bajo ``sky_claw/app/orchestrator/``, que el
    override amplio ``sky_claw.app.orchestrator.*`` marca con
    ``ignore_errors = true``. Sin una entrada estricta *explícita* por módulo,
    ``mypy sky_claw/`` los tapa: el gate bloqueante saldría verde aunque estos
    módulos de producción tuvieran errores de tipos reales (exactamente lo que
    reprodujo el P1: ``source`` inferido como ``str`` en vez del ``Literal``
    ``PluginListSource``).

    Este test parsea ``pyproject.toml`` estructuralmente (no por substring) y
    exige que ambos módulos figuren en un override ``strict = true`` /
    ``ignore_errors = false``. Si alguien los quita de ahí, caen bajo el wildcard
    ``ignore_errors``: ``mypy`` volvería a quedar verde por exención, pero este
    test FALLA. La regla del hermano aplica: los dos módulos del par se congelan
    juntos, no uno solo.
    """
    with (REPO_ROOT / "pyproject.toml").open("rb") as file:
        pyproject = tomllib.load(file)

    target_modules = {
        "sky_claw.app.orchestrator.active_plugins",
        "sky_claw.app.orchestrator.plugin_limit_guard",
    }

    strict_modules: set[str] = set()
    ignore_modules: set[str] = set()
    for override in pyproject["tool"]["mypy"]["overrides"]:
        modules = override["module"]
        if override.get("strict") is True and override.get("ignore_errors") is False:
            strict_modules.update(modules)
        if override.get("ignore_errors") is True:
            ignore_modules.update(modules)
            # Nunca listados literalmente en un override de exención (sería
            # contradictorio con su membresía estricta).
            assert target_modules.isdisjoint(modules)

    # El riesgo es real: el wildcard que los taparía existe y está en modo
    # exención. Si esto deja de ser cierto, el guardrail pierde su motivo — pero
    # entonces tampoco habría exención silenciosa que atajar.
    assert "sky_claw.app.orchestrator.*" in ignore_modules

    # Núcleo del guardrail: ambos seams PR5 deben estar bajo mypy estricto.
    assert target_modules <= strict_modules


def test_el_parser_de_pe_es_dependencia_dura_no_opcional() -> None:
    """Sin ``pefile`` declarado, el gate de runtime de SKSE deja de proteger y pasa a bloquear.

    ``scanner._read_pe_product_version`` importa ``pefile`` dentro de un
    ``try/ImportError`` y devuelve ``None`` si falta, así que ``read_skyrim_version()``
    entrega ``""`` **sin error visible**. ``ensure_skse`` es fail-closed ante esa
    versión vacía: si el parser no está, no rechaza el caso dudoso — los rechaza
    TODOS, y el autoinstalador queda inalcanzable en el entorno documentado
    (``uv sync --extra dev``). Un fail-closed sobre una señal que nunca llega no es
    fail-closed.

    Se ancla en las tres capas porque cada una se rompe distinto: declarada en
    ``[project.dependencies]`` (no en ``[dev]``, que no se instala en producción),
    resuelta en ``uv.lock`` (o un ``uv sync`` la deja afuera) e importable acá (o la
    declaración quedó de adorno).

    *Historia: el gate se escribió, la suite entera stubeaba ``read_skyrim_version``,
    CI dio verde en las cuatro combinaciones de OS × Python, y el defecto lo encontró
    un revisor automático leyendo el árbol de dependencias — no un test.*
    """
    with (REPO_ROOT / "pyproject.toml").open("rb") as file:
        pyproject = tomllib.load(file)

    runtime = [Requirement(d).name for d in pyproject["project"]["dependencies"]]
    assert "pefile" in runtime, "pefile tiene que ser dependencia de runtime, no de [dev]"

    uv_lock = (REPO_ROOT / "uv.lock").read_text(encoding="utf-8")
    assert 'name = "pefile"' in uv_lock, "pefile declarado pero sin resolver en uv.lock"

    import pefile  # noqa: PLC0415 — el import ES la aserción: tiene que estar instalado

    assert pefile is not None


def test_la_lectura_de_version_llega_al_parser_y_no_a_la_rama_de_importerror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """El camino real de lectura de versión, SIN stub — el hueco que dejó pasar el defecto.

    Todos los tests de ``ensure_skse`` monkeypatchean ``read_skyrim_version``, así que
    ninguno ejercitaba la función posta y el "siempre devuelve vacío" era invisible.

    La aserción no es sobre el valor devuelto (haría falta un PE real con recurso de
    versión), sino sobre **qué rama se toma**: se sustituye ``pefile.PE`` por un doble
    que registra su invocación. Si el helper hubiera caído en el ``except ImportError``
    —que es lo que pasaba sin la dependencia declarada— el doble nunca se llama y el
    test falla nombrando la causa. Los dos desenlaces devuelven ``None`` por fuera, así
    que distinguirlos requiere mirar el camino, no el resultado.
    """
    import pefile  # noqa: PLC0415

    from sky_claw.local.discovery import scanner  # noqa: PLC0415

    invocaciones: list[str] = []

    class _PEDoble:
        def __init__(self, path: str, fast_load: bool = True) -> None:
            invocaciones.append(path)

        def parse_data_directories(self, directories: object) -> None:
            return None

        def close(self) -> None:
            return None

    monkeypatch.setattr(pefile, "PE", _PEDoble)

    exe = tmp_path / "SkyrimSE.exe"
    exe.write_bytes(b"MZ")
    scanner.read_skyrim_version(exe)

    assert invocaciones == [str(exe)], (
        "read_skyrim_version no llegó al parser de PE: cayó en el except ImportError, "
        "que es exactamente el estado en el que el gate de ensure_skse bloquea todo"
    )

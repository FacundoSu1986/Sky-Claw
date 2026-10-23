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


def _versiones_pineadas(lock: str) -> dict[str, str]:
    """Paquetes ``nombre==versión`` de un ``requirements*.lock`` (nombres normalizados)."""
    return {
        nombre.lower().replace("_", "-"): version_pineada
        for nombre, version_pineada in re.findall(r"(?m)^([A-Za-z0-9_.\-]+)==([^\s\\]+)", lock)
    }


def _divergencias_entre_locks(a: str, b: str) -> dict[str, tuple[str, str]]:
    """Paquetes presentes en ambos locks con distinta versión: ``{nombre: (versión_a, versión_b)}``."""
    pines_a, pines_b = _versiones_pineadas(a), _versiones_pineadas(b)
    return {
        nombre: (pines_a[nombre], pines_b[nombre])
        for nombre in pines_a.keys() & pines_b.keys()
        if pines_a[nombre] != pines_b[nombre]
    }


def _faltantes_en_el_extra(base: str, extra: str) -> set[str]:
    """Paquetes del lock base que el lock del extra no trae: el extra es un superconjunto por construcción."""
    return _versiones_pineadas(base).keys() - _versiones_pineadas(extra).keys()


def test_los_dos_locks_de_requirements_pinean_las_mismas_versiones() -> None:
    """Subir un paquete en un lock y no en su gemelo deja a CI auditando solo uno de los dos.

    ``requirements.lock`` y ``requirements-native-parallax.lock`` salen del mismo ``pyproject.toml``
    (el segundo con ``--extra native-parallax``) y CI los audita en pasos separados con ``pip-audit
    --strict``: el segundo paso no corre si el primero falla. Un aviso nuevo sobre un paquete común
    (certifi, GHSA-6q48-6c7w-9gw2) obliga a subirlo en los dos; si solo se sube uno, el otro falla
    recién en la corrida siguiente. Se enumeran TODOS los paquetes del lock base, no solo certifi.
    """
    base = (REPO_ROOT / "requirements.lock").read_text(encoding="utf-8")
    extra = (REPO_ROOT / "requirements-native-parallax.lock").read_text(encoding="utf-8")

    pines_base = _versiones_pineadas(base)

    # guarda contra el pase vacío: si el formato de los locks cambiara y el parseo no devolviera nada, las
    # dos aserciones de abajo pasarían sin comparar un solo paquete
    assert pines_base, "no se leyó ningún paquete pineado de requirements.lock"
    # el extra solo AGREGA (numpy, pillow): si una regeneración le suelta un paquete base, la intersección lo
    # excluiría en silencio y la instalación del extra quedaría incompleta con todo lo demás en verde
    assert _faltantes_en_el_extra(base, extra) == set()
    assert _divergencias_entre_locks(base, extra) == {}


def test_el_ancla_de_locks_detecta_una_divergencia() -> None:
    """Sin este caso el ancla de arriba podría pasar vacía (regex que no matchea nada)."""
    viejo = "certifi==2026.2.25 \\\n    --hash=sha256:aa\nidna==3.15 \\\n    --hash=sha256:bb\n"
    nuevo = "certifi==2026.10.19 \\\n    --hash=sha256:cc\nidna==3.15 \\\n    --hash=sha256:bb\nnumpy==2.4.6\n"

    assert _divergencias_entre_locks(viejo, nuevo) == {"certifi": ("2026.2.25", "2026.10.19")}
    assert _divergencias_entre_locks(viejo, viejo) == {}
    # un paquete base que el extra no trae no es una «divergencia» (no está en la intersección): lo atrapa el otro
    assert _faltantes_en_el_extra(viejo, nuevo) == set()
    assert _faltantes_en_el_extra(nuevo, "certifi==2026.10.19\n") == {"idna", "numpy"}


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


def test_seams_pr5_pr6_orchestrator_bajo_mypy_strict() -> None:
    """Los seams PR5/PR6 nacen bajo mypy estricto y no quedan silenciosamente exentos.

    Guardrail hermético para el P1 de #540: ``active_plugins``,
    ``plugin_limit_guard`` y ``record_conflict_scan`` viven bajo
    ``sky_claw/app/orchestrator/``, que el override amplio
    ``sky_claw.app.orchestrator.*`` marca con ``ignore_errors = true``. Sin una
    entrada estricta *explícita* por módulo, ``mypy sky_claw/ scripts/`` los tapa: el gate
    bloqueante saldría verde aunque estos módulos de producción tuvieran errores
    de tipos reales (exactamente lo que reprodujo el P1: ``source`` inferido como
    ``str`` en vez del ``Literal`` ``PluginListSource`` — el mismo patrón que
    ``record_conflict_scan`` evita con su tuple anotado).

    Este test parsea ``pyproject.toml`` estructuralmente (no por substring) y
    exige que los tres módulos figuren en un override ``strict = true`` /
    ``ignore_errors = false``. Si alguien los quita de ahí, caen bajo el wildcard
    ``ignore_errors``: ``mypy`` volvería a quedar verde por exención, pero este
    test FALLA. La regla del hermano aplica: la familia se congela junta, no un
    módulo suelto.
    """
    with (REPO_ROOT / "pyproject.toml").open("rb") as file:
        pyproject = tomllib.load(file)

    target_modules = {
        "sky_claw.app.orchestrator.active_plugins",
        "sky_claw.app.orchestrator.plugin_limit_guard",
        "sky_claw.app.orchestrator.record_conflict_scan",
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

    # Núcleo del guardrail: todos los seams PR5/PR6 bajo mypy estricto.
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


def test_comtypes_es_dependencia_win32_declarada_y_resuelta() -> None:
    """El backend UIA real existe en los manifests, no sólo en un import perezoso.

    Espejo del ancla de ``pefile`` con una diferencia deliberada: ``comtypes``
    es Windows-only, así que NO se afirma importabilidad acá (en el CI de
    Ubuntu no está instalado por el marker). Lo que sí se ancla en las tres
    capas: declarado en ``[project.dependencies]`` con marker ``win32`` (no en
    ``[dev]``, que no se instala en producción), resuelto en ``uv.lock`` (o un
    ``uv sync`` en Windows lo deja afuera del rig) y pineado con hashes en
    ``requirements.lock``.

    Sin esto, un ``pyproject`` que declara y un lock que no resuelve dan un rig
    Windows donde ``construir_observador_windows`` falla cerrado SIEMPRE — un
    gate que nunca puede dar MATCH.
    """
    with (REPO_ROOT / "pyproject.toml").open("rb") as file:
        pyproject = tomllib.load(file)

    dependencias = [Requirement(d) for d in pyproject["project"]["dependencies"]]
    comtypes = [d for d in dependencias if d.name == "comtypes"]
    assert len(comtypes) == 1, "comtypes tiene que ser dependencia de runtime, no de [dev]"
    assert comtypes[0].marker is not None and "win32" in str(comtypes[0].marker), (
        f"comtypes sin marker win32 rompería el CI de Ubuntu: {comtypes[0]}"
    )

    uv_lock = (REPO_ROOT / "uv.lock").read_text(encoding="utf-8")
    assert 'name = "comtypes"' in uv_lock, "comtypes declarado pero sin resolver en uv.lock"

    requirements_lock = (REPO_ROOT / "requirements.lock").read_text(encoding="utf-8")
    assert re.search(r"(?m)^comtypes==", requirements_lock), "comtypes declarado pero sin pineado en requirements.lock"


def test_anyio_piso_de_seguridad_declarado_y_bloqueado() -> None:
    """AnyIO debe mantener el piso seguro >=4.14.2 ante CVE-2026-63374 y CVE-2026-64847.

    Previene regresiones donde una actualización o resolución accidental baje
    anyio a 4.13.0 u otra versión vulnerable en pyproject.toml, requirements.lock
    o uv.lock.
    """
    from packaging.version import Version

    piso_minimo = Version("4.14.2")

    # 1. pyproject.toml
    with (REPO_ROOT / "pyproject.toml").open("rb") as file:
        pyproject = tomllib.load(file)

    runtime_deps = [Requirement(d) for d in pyproject["project"]["dependencies"]]
    candidatos = [d for d in runtime_deps if d.name == "anyio"]
    assert len(candidatos) == 1, f"se esperaba exactamente un anyio en runtime, hay {candidatos}"
    req = candidatos[0]
    assert req.specifier == SpecifierSet(">=4.14.2,<5"), f"rango de anyio inesperado: {req.specifier}"

    # 2. requirements.lock
    req_match = re.search(r"(?m)^anyio==([^\s\\]+)", (REPO_ROOT / "requirements.lock").read_text(encoding="utf-8"))
    assert req_match is not None, "anyio no encontrado en requirements.lock"
    version_req = Version(req_match.group(1))
    assert version_req >= piso_minimo, f"requirements.lock tiene anyio {version_req} < {piso_minimo}"

    # 3. uv.lock
    with (REPO_ROOT / "uv.lock").open("rb") as file:
        uv_data = tomllib.load(file)
    paquetes_anyio = [p for p in uv_data.get("package", []) if p.get("name") == "anyio"]
    assert len(paquetes_anyio) == 1, f"se esperaba 1 paquete anyio en uv.lock, hay {len(paquetes_anyio)}"
    version_uv = Version(paquetes_anyio[0]["version"])
    assert version_uv >= piso_minimo, f"uv.lock tiene anyio {version_uv} < {piso_minimo}"


def test_multidict_piso_de_seguridad_declarado_y_bloqueado() -> None:
    """multidict debe mantener el piso seguro >=6.9.1 ante CVE-2026-104874.

    Previene regresiones donde una actualización o resolución accidental baje
    multidict a 6.7.1 u otra versión vulnerable en pyproject.toml, requirements.lock
    o uv.lock.
    """
    from packaging.version import Version

    # 1. pyproject.toml
    with (REPO_ROOT / "pyproject.toml").open("rb") as file:
        pyproject = tomllib.load(file)

    runtime_deps = [Requirement(d) for d in pyproject["project"]["dependencies"]]
    candidatos = [d for d in runtime_deps if d.name == "multidict"]
    assert len(candidatos) == 1, f"se esperaba exactamente un multidict en runtime, hay {candidatos}"
    req = candidatos[0]
    assert req.specifier == SpecifierSet(">=6.9.1,<7"), f"rango de multidict inesperado: {req.specifier}"

    # 2. requirements.lock
    matches_req = re.findall(
        r"(?m)^multidict==([^\s\\]+)", (REPO_ROOT / "requirements.lock").read_text(encoding="utf-8")
    )
    assert len(matches_req) == 1, (
        f"se esperaba exactamente una entrada de multidict en requirements.lock, hay {matches_req}"
    )
    version_req = Version(matches_req[0])
    assert version_req in req.specifier, (
        f"requirements.lock tiene multidict {version_req} fuera del rango {req.specifier}"
    )

    # 3. uv.lock
    with (REPO_ROOT / "uv.lock").open("rb") as file:
        uv_data = tomllib.load(file)
    paquetes_multidict = [p for p in uv_data.get("package", []) if p.get("name") == "multidict"]
    assert len(paquetes_multidict) == 1, (
        f"se esperaba exactamente 1 paquete multidict en uv.lock, hay {len(paquetes_multidict)}"
    )
    version_uv = Version(paquetes_multidict[0]["version"])
    assert version_uv in req.specifier, f"uv.lock tiene multidict {version_uv} fuera del rango {req.specifier}"


def test_certifi_piso_de_seguridad_declarado_y_bloqueado() -> None:
    """certifi debe mantener el piso seguro >=2026.10.19 ante GHSA-6q48-6c7w-9gw2.

    Sin el piso en ``pyproject.toml``, un ``pip install -e .`` sobre un entorno existente queda satisfecho
    por la 2026.2.25 vulnerable aunque los locks ya estén subidos. Se verifican los TRES locks que pinean
    certifi (los tests de anyio y multidict no miran ``requirements-native-parallax.lock``).
    """
    from packaging.version import Version

    piso_minimo = Version("2026.10.19")

    # 1. pyproject.toml
    with (REPO_ROOT / "pyproject.toml").open("rb") as file:
        pyproject = tomllib.load(file)
    runtime_deps = [Requirement(d) for d in pyproject["project"]["dependencies"]]
    candidatos = [d for d in runtime_deps if d.name == "certifi"]
    assert len(candidatos) == 1, f"se esperaba exactamente un certifi en runtime, hay {candidatos}"
    req = candidatos[0]
    assert not req.specifier.contains(Version("2026.2.25")), f"el rango de certifi admite la versión vulnerable: {req}"
    assert req.specifier.contains(piso_minimo), f"el rango de certifi no cubre el piso {piso_minimo}: {req}"

    # 2. los dos requirements*.lock
    for nombre in ("requirements.lock", "requirements-native-parallax.lock"):
        pines = _versiones_pineadas((REPO_ROOT / nombre).read_text(encoding="utf-8"))
        assert "certifi" in pines, f"certifi no está pineado en {nombre}"
        assert Version(pines["certifi"]) >= piso_minimo, f"{nombre} tiene certifi {pines['certifi']} < {piso_minimo}"

    # 3. uv.lock
    with (REPO_ROOT / "uv.lock").open("rb") as file:
        uv_data = tomllib.load(file)
    paquetes_certifi = [p for p in uv_data.get("package", []) if p.get("name") == "certifi"]
    assert len(paquetes_certifi) == 1, f"se esperaba 1 paquete certifi en uv.lock, hay {len(paquetes_certifi)}"
    version_uv = Version(paquetes_certifi[0]["version"])
    assert version_uv >= piso_minimo, f"uv.lock tiene certifi {version_uv} < {piso_minimo}"


#: Los paquetes de OpenTelemetry que declara el runtime; el resto de la familia lo arrastran ellos.
_OPENTELEMETRY_DECLARADOS = ("opentelemetry-api", "opentelemetry-sdk", "opentelemetry-exporter-otlp-proto-grpc")


def test_opentelemetry_piso_de_seguridad_declarado_y_bloqueado() -> None:
    """La familia OpenTelemetry debe mantener el piso seguro >=1.45.1 ante CVE-2026-114603 (opentelemetry-api).

    La familia versiona junta (api, sdk, exporters, proto y common salen con el mismo número), así que se enumeran
    TODOS los ``opentelemetry-*`` de la serie 1.x de los TRES locks, no solo el paquete del aviso: un gemelo que quedara
    atrás dejaría instalada otra versión vieja de la misma familia. ``semantic-conventions`` va en su propia serie 0.x y
    no entra en la comparación.
    """
    from packaging.version import Version

    piso_minimo = Version("1.45.1")
    vulnerable = Version("1.41.1")

    # 1. pyproject.toml: el piso en los tres declarados
    with (REPO_ROOT / "pyproject.toml").open("rb") as file:
        pyproject = tomllib.load(file)
    runtime_deps = [Requirement(d) for d in pyproject["project"]["dependencies"]]
    for nombre in _OPENTELEMETRY_DECLARADOS:
        candidatos = [d for d in runtime_deps if d.name == nombre]
        assert len(candidatos) == 1, f"se esperaba exactamente un {nombre} en runtime, hay {candidatos}"
        req = candidatos[0]
        # Igualdad exacta (como anyio y multidict): probar dos puntos del rango no alcanza, porque el aviso afecta
        # hasta 1.45.0 y un piso >=1.42 rechazaría 1.41.1 y aceptaría 1.45.1 dejando pasar 1.45.0.
        assert req.specifier == SpecifierSet(">=1.45.1,<2"), f"rango de {nombre} inesperado: {req.specifier}"
        assert not req.specifier.contains(vulnerable), f"el rango de {nombre} admite la versión vulnerable: {req}"

    # 2. los dos requirements*.lock: toda la familia 1.x
    for archivo in ("requirements.lock", "requirements-native-parallax.lock"):
        pines = _versiones_pineadas((REPO_ROOT / archivo).read_text(encoding="utf-8"))
        familia = {n: Version(v) for n, v in pines.items() if n.startswith("opentelemetry-") and Version(v).major >= 1}
        assert set(_OPENTELEMETRY_DECLARADOS) <= set(familia), f"{archivo} no pinea toda la familia declarada"
        viejos = {n: str(v) for n, v in familia.items() if v < piso_minimo}
        assert viejos == {}, f"{archivo} tiene paquetes de OpenTelemetry por debajo de {piso_minimo}: {viejos}"

    # 3. uv.lock: toda la familia 1.x
    with (REPO_ROOT / "uv.lock").open("rb") as file:
        uv_data = tomllib.load(file)
    familia_uv = [
        (p["name"], Version(p["version"]))
        for p in uv_data.get("package", [])
        if p.get("name", "").startswith("opentelemetry-") and Version(p["version"]).major >= 1
    ]
    assert set(_OPENTELEMETRY_DECLARADOS) <= {n for n, _ in familia_uv}, "uv.lock no resuelve toda la familia declarada"
    viejos_uv = {n: str(v) for n, v in familia_uv if v < piso_minimo}
    assert viejos_uv == {}, f"uv.lock tiene paquetes de OpenTelemetry por debajo de {piso_minimo}: {viejos_uv}"


def test_brotli_piso_de_seguridad_declarado_y_bloqueado() -> None:
    """brotli debe mantener el piso seguro >=1.2.1 ante PYSEC-2026-4322 (``pip-audit --strict`` sobre 1.2.0).

    brotli llega por ``py7zr`` (solo en CPython; en PyPy lo reemplaza ``brotlicffi``), así que sin el piso en
    ``pyproject.toml`` un ``pip install -e .`` sobre un entorno existente queda satisfecho por la 1.2.0 vulnerable aunque
    los locks ya estén subidos. Se verifican los TRES locks que lo pinean, igual que certifi.
    """
    from packaging.version import Version

    piso_minimo = Version("1.2.1")
    vulnerable = Version("1.2.0")

    # 1. pyproject.toml: igualdad exacta del rango (como anyio y multidict) y el marcador que lo limita a CPython
    with (REPO_ROOT / "pyproject.toml").open("rb") as file:
        pyproject = tomllib.load(file)
    runtime_deps = [Requirement(d) for d in pyproject["project"]["dependencies"]]
    candidatos = [d for d in runtime_deps if d.name == "brotli"]
    assert len(candidatos) == 1, f"se esperaba exactamente un brotli en runtime, hay {candidatos}"
    req = candidatos[0]
    assert req.specifier == SpecifierSet(">=1.2.1,<2"), f"rango de brotli inesperado: {req.specifier}"
    assert not req.specifier.contains(vulnerable), f"el rango de brotli admite la versión vulnerable: {req}"
    assert req.marker is not None and req.marker.evaluate({"platform_python_implementation": "CPython"})
    assert not req.marker.evaluate({"platform_python_implementation": "PyPy"}), (
        "brotli no puede exigirse en PyPy: py7zr usa brotlicffi ahí"
    )

    # 2. los dos requirements*.lock
    for nombre in ("requirements.lock", "requirements-native-parallax.lock"):
        pines = _versiones_pineadas((REPO_ROOT / nombre).read_text(encoding="utf-8"))
        assert "brotli" in pines, f"brotli no está pineado en {nombre}"
        assert Version(pines["brotli"]) >= piso_minimo, f"{nombre} tiene brotli {pines['brotli']} < {piso_minimo}"

    # 3. uv.lock
    with (REPO_ROOT / "uv.lock").open("rb") as file:
        uv_data = tomllib.load(file)
    paquetes_brotli = [p for p in uv_data.get("package", []) if p.get("name") == "brotli"]
    assert len(paquetes_brotli) == 1, f"se esperaba 1 paquete brotli en uv.lock, hay {len(paquetes_brotli)}"
    version_uv = Version(paquetes_brotli[0]["version"])
    assert version_uv >= piso_minimo, f"uv.lock tiene brotli {version_uv} < {piso_minimo}"


def test_pillow_es_dependencia_dev_declarada_para_research_parallax() -> None:
    """Pillow tiene que estar declarado en ``[dev]`` para el research de native_parallax.

    ``authored_dataset.py`` (EXP-M2+) importa ``PIL`` a nivel de módulo y su suite
    también: sin la declaración, ``test_native_parallax_exp_m2.py`` ni siquiera
    colecciona en un entorno donde Pillow no venga ya instalado. Espejo inverso
    del ancla de ``pefile``: acá la dependencia es dev-only para research (ningún
    camino de producción importa ``sky_claw.local.native_parallax.research``) y del
    extra opcional ``native-parallax`` para el producto (ADR 0014), así que NO debe
    aparecer en runtime. El rango del extra lo ancla
    ``test_native_parallax_producto_invariant.py`` (igual al de ``[dev]``).

    *Historia: el hueco quedó invisible porque la rama de #620 nunca corrió CI
    (su base era una rama de feature, fuera del trigger ``pull_request``), y el
    defecto se descubrió restackeando contra main.*
    """
    from packaging.version import Version

    with (REPO_ROOT / "pyproject.toml").open("rb") as file:
        pyproject = tomllib.load(file)

    dev = [Requirement(d) for d in pyproject["project"]["optional-dependencies"]["dev"]]
    pillow = [d for d in dev if d.name == "pillow"]
    assert len(pillow) == 1, f"se esperaba exactamente un pillow en [dev], hay {len(pillow)}"

    # pillow>=13.0,<14. Piso: pip-audit --strict reporta 8 avisos nuevos sobre 12.3.0 (CVE-2026-112395, -112397,
    # -112400, -112404, -112895, -112896, -112897, -112899) y todos están corregidos en 13.0.0 (antes, 35 avisos
    # sobre 11.3.0 corregidos en 12.3.0; el lock del extra native-parallax se audita en CI). Techo: la próxima
    # mayor no está verificada (la suite de native_parallax se corrió contra 13.0.0).
    assert pillow[0].specifier.contains(Version("13.0")), f"el rango de pillow no cubre 13.0: {pillow[0].specifier}"
    assert pillow[0].specifier.contains(Version("13.9")), f"el rango de pillow no cubre 13.x: {pillow[0].specifier}"
    assert not pillow[0].specifier.contains(Version("12.3")), (
        f"el rango de pillow admite una versión con avisos de seguridad: {pillow[0].specifier}"
    )
    assert not pillow[0].specifier.contains(Version("14.0")), (
        f"el rango de pillow no corta en 14: {pillow[0].specifier}"
    )

    runtime = [Requirement(d).name for d in pyproject["project"]["dependencies"]]
    assert "pillow" not in runtime, "pillow es dev-only (research) o del extra opcional: no debe estar en runtime"

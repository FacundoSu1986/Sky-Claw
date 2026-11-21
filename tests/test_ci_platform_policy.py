"""Anclas de la política de plataformas del workflow principal.

La aplicación completa se certifica en Windows. Linux/WSL2 conserva una señal
informativa y acotada para las ramas POSIX del núcleo async documentadas en
``DEPLOYMENT.md``; no vuelve a ejecutar la suite completa.
"""

from __future__ import annotations

import ast
import re
import shlex
from pathlib import Path
from typing import Any, cast

import yaml

RAIZ = Path(__file__).resolve().parents[1]
WORKFLOW_CI = RAIZ / ".github" / "workflows" / "ci.yml"

OBJETIVOS_POSIX = [
    "tests/test_path_validator.py",
    "tests/test_preflight_wiring.py",
    "tests/test_dyndolod_uia_preflight.py::test_el_observador_por_defecto_falla_cerrado_en_vez_de_adivinar",
    "tests/test_dyndolod_uia_preflight.py::test_el_pipeline_con_el_observador_por_defecto_da_unknown_no_error",
    "tests/test_dyndolod_uia_preflight.py::test_uia_no_disponible_es_un_error_uia",
    "tests/test_dyndolod_uia_preflight.py::test_el_modulo_no_importa_nada_de_windows",
    "tests/test_frozen_runtime_p4_root_lock.py",
    "tests/test_etapa6_testigo_durable.py",
]


def _jobs() -> dict[str, Any]:
    workflow = yaml.safe_load(WORKFLOW_CI.read_text(encoding="utf-8"))
    return cast(dict[str, Any], workflow["jobs"])


def _concurrency() -> dict[str, Any]:
    """Carga y devuelve la sección `concurrency` de ci.yml."""
    workflow = yaml.safe_load(WORKFLOW_CI.read_text(encoding="utf-8"))
    return cast(dict[str, Any], workflow["concurrency"])


def _paso_por_id(job: dict[str, Any], step_id: str) -> dict[str, Any]:
    coincidencias = [step for step in job["steps"] if step.get("id") == step_id]
    assert len(coincidencias) == 1, f"se esperaba exactamente un step con id={step_id!r}"
    return cast(dict[str, Any], coincidencias[0])


def _paso_por_nombre(job: dict[str, Any], nombre: str) -> dict[str, Any]:
    coincidencias = [step for step in job["steps"] if step.get("name") == nombre]
    assert len(coincidencias) == 1, f"se esperaba exactamente un step llamado {nombre!r}"
    return cast(dict[str, Any], coincidencias[0])


def test_lint_typecheck_y_seguridad_incluyen_scripts_con_comandos_anclados() -> None:
    jobs = _jobs()
    lint = jobs["lint"]
    typecheck = jobs["typecheck"]
    security = jobs["security"]

    assert _paso_por_nombre(lint, "Ruff check")["run"] == (
        "ruff check sky_claw/ scripts/ tests/ --output-format=github"
    )
    assert _paso_por_nombre(lint, "Ruff format check")["run"] == "ruff format --check sky_claw/ scripts/ tests/"
    assert _paso_por_nombre(typecheck, "Mypy check")["run"] == (
        "python -m mypy sky_claw/ scripts/ --ignore-missing-imports --no-error-summary"
    )
    assert _paso_por_nombre(security, "Bandit (SAST)")["run"] == "bandit -r sky_claw/ scripts/ -ll"


#: Cantidad de shards en que se reparte la suite completa de Windows. Cambiarla exige tocar, a la vez, la matriz
#: ``shard`` del job ``test-shard``, ``SKYCLAW_SHARD`` y el ``--total`` del verificador: el test de consistencia de abajo
#: ata las tres cosas a este número.
TOTAL_DE_SHARDS = 4

#: Umbral de cobertura de la suite completa (``CONTRIBUTING.md`` y ``.github/coding_conventions.md``): sigue siendo un gate
#: bloqueante aunque ahora se mida sobre los datos combinados de todos los shards.
UMBRAL_DE_COBERTURA = 60


def _lineas_de_run(paso: dict[str, Any]) -> list[str]:
    """Las órdenes de un ``run`` multilínea, sin espacios sobrantes ni líneas vacías."""
    return [linea.strip() for linea in str(paso["run"]).splitlines() if linea.strip()]


def test_la_suite_completa_solo_certifica_windows() -> None:
    jobs = _jobs()
    shard = jobs["test-shard"]
    veredicto = jobs["test"]

    assert shard["strategy"]["matrix"] == {
        "os": ["windows-latest"],
        "python-version": ["3.11", "3.12"],
        "shard": list(range(1, TOTAL_DE_SHARDS + 1)),
    }
    assert veredicto["strategy"]["matrix"] == {
        "os": ["windows-latest"],
        "python-version": ["3.11", "3.12"],
    }
    assert "continue-on-error" not in shard
    assert "continue-on-error" not in veredicto
    assert veredicto["needs"] == ["test-shard"]
    assert jobs["test-summary"]["name"] == "🧪 Tests (Pytest)"
    assert jobs["test-summary"]["needs"] == ["test"]
    assert "test-summary" in jobs["build"]["needs"]


def test_el_veredicto_por_version_conserva_el_nombre_que_la_proteccion_de_rama_pudo_exigir() -> None:
    """El check por versión sigue llamándose igual: partir la suite no puede dejar un check requerido «esperando»."""
    assert _jobs()["test"]["name"] == "🧪 Tests / ${{ matrix.os }} / py${{ matrix.python-version }}"


#: Piso del ``timeout-minutes`` del job ``test-shard`` (el que ahora corre la suite). Se mantiene en el valor que la suite
#: COMPLETA necesitó en un solo job: cada shard hace ~1/N del trabajo, pero ningún shard medido todavía justifica bajarlo.
#: Baja solo a propósito y con duraciones de shard medidas: un valor menor reintroduce la cancelación sin ningún test fallando.
TOPE_MINIMO_DE_LA_MATRIZ_WINDOWS = 90


def test_el_tope_de_la_matriz_windows_cubre_el_peor_caso_medido() -> None:
    """El job que corre la suite no puede quedar por debajo de lo que la suite llegó a tardar en el runner más lento.

    2026-11-15: con 70 min, ``Windows py3.11`` se canceló tres veces seguidas (#935 dos veces sobre el mismo head, #936 una)
    con la suite al 98 % y ningún test fallando; el peor caso extrapolado es ~72 min y ``py3.12`` ronda 50-62 min. El
    agregador ``Tests (Pytest)`` solo ve ese ``cancelled`` y bloquea el merge de un PR cuyo código está sano.
    """
    tope = _jobs()["test-shard"]["timeout-minutes"]

    assert isinstance(tope, int)
    assert not isinstance(tope, bool)
    assert tope >= TOPE_MINIMO_DE_LA_MATRIZ_WINDOWS, (
        f"timeout-minutes del job test-shard es {tope}: la suite ya tardó más de 70 min en Windows py3.11; "
        f"el piso es {TOPE_MINIMO_DE_LA_MATRIZ_WINDOWS} (ver el comentario en ci.yml antes de bajarlo)"
    )


def test_cada_shard_corre_su_parte_con_cobertura_y_deja_su_manifiesto() -> None:
    shard = _jobs()["test-shard"]
    paso = _paso_por_nombre(shard, "Run test shard")

    assert paso["env"] == {
        "SKYCLAW_SHARD": f"${{{{ matrix.shard }}}}/{TOTAL_DE_SHARDS}",
        "SKYCLAW_SHARD_MANIFEST": "shard-out/shard-manifest.json",
        "COVERAGE_FILE": "shard-out/coverage.sqlite",
    }
    assert _lineas_de_run(paso) == [
        "New-Item -ItemType Directory -Force shard-out | Out-Null",
        "python -m pytest -p scripts.ci_particion --cov=sky_claw --cov=scripts --cov-report= tests/",
    ]
    # Sin ``shell``: PowerShell, el shell por defecto de Windows y el que usaba el job único. Con ``bash`` dos tests que
    # asumen un proceso sin privilegios fallaron en el primer ensayo de CI (#966) y con PowerShell pasan: el resultado
    # cambia con el shell, y no se cambia el entorno de la suite sin necesidad.
    assert "shell" not in paso
    subida = _paso_por_nombre(shard, "Upload shard results")
    assert subida["if"] == "always()"
    assert subida["with"]["name"] == "test-shard-py${{ matrix.python-version }}-${{ matrix.shard }}"
    assert subida["with"]["path"] == "shard-out/"


def test_los_artefactos_intermedios_de_los_shards_duran_un_dia() -> None:
    """Son datos que el veredicto consume en la MISMA corrida; con la retención por defecto (90 días) 8 artefactos por
    corrida y por push llenarían la cuota de almacenamiento, que el ``.exe`` de ~88 MB del job Build ya presiona."""
    subida = _paso_por_nombre(_jobs()["test-shard"], "Upload shard results")

    assert subida["with"]["retention-days"] == 1


def test_un_shard_rojo_deja_rojo_el_veredicto_en_vez_de_omitirlo() -> None:
    """GitHub da por satisfecho un check requerido que se omitió: con el ``needs`` por defecto, un shard que falla dejaría
    el veredicto por versión en «skipped» en lugar de rojo. ``!cancelled()`` lo hace correr y su primer paso lo falla."""
    veredicto = _jobs()["test"]
    primero = veredicto["steps"][0]

    assert veredicto["if"] == "${{ !cancelled() }}"
    assert primero["name"] == "Require every shard to have passed"
    assert primero["shell"] == "bash"
    # El guard COMPLETO, no solo sus ingredientes: con ``== "success"`` en vez de ``!= "success"`` seguirían estando
    # ``needs.test-shard.result`` y ``exit 1`` en el texto, pero un shard rojo dejaría pasar y uno verde fallaría.
    assert _lineas_de_run(primero) == [
        'if [[ "${{ needs.test-shard.result }}" != "success" ]]; then',
        'echo "Test shards failed: ${{ needs.test-shard.result }}"',
        "exit 1",
        "fi",
    ]


def test_el_veredicto_verifica_que_no_se_perdio_ningun_test_y_exige_la_cobertura_combinada() -> None:
    veredicto = _jobs()["test"]
    descarga = next(p for p in veredicto["steps"] if str(p.get("uses", "")).startswith("actions/download-artifact@"))
    subida_de_shard = _paso_por_nombre(_jobs()["test-shard"], "Upload shard results")

    # El patrón de descarga es exactamente el prefijo con que cada shard sube lo suyo (para la MISMA versión de Python).
    prefijo = str(subida_de_shard["with"]["name"]).rsplit("-", 1)[0] + "-"
    assert descarga["with"] == {"pattern": f"{prefijo}*", "path": "shards"}

    # ``--con-cobertura``: la cobertura combinada solo es la de toda la suite si CADA shard aportó su base; ``coverage
    # combine`` sobre un glob con una base de menos no falla y con las restantes puede igual cruzar el umbral.
    assert _lineas_de_run(_paso_por_nombre(veredicto, "Verify shard completeness")) == [
        f"python -m scripts.ci_particion verificar shards --total {TOTAL_DE_SHARDS} --con-cobertura",
    ]
    # ``xml`` ANTES de ``report --fail-under``: el umbral hace salir con error al ``report`` y, con ``bash -e``, lo que
    # viene después no corre; el XML es justo lo que permite ver qué líneas no se cubren cuando el umbral falla.
    assert _lineas_de_run(_paso_por_nombre(veredicto, "Combine coverage and enforce threshold")) == [
        "python -m coverage combine shards/*/coverage.sqlite",
        "python -m coverage xml",
        f"python -m coverage report --fail-under={UMBRAL_DE_COBERTURA}",
    ]

    # El artefacto de cobertura conserva su nombre y su ruta: nada fuera de ci.yml tuvo que enterarse del reparto.
    cobertura = _paso_por_nombre(veredicto, "Upload coverage")
    assert cobertura["with"] == {
        "name": "coverage-report-${{ matrix.os }}-py${{ matrix.python-version }}",
        "path": "coverage.xml",
        "overwrite": True,
    }


def _subidas_que_corren_aun_si_el_job_falla() -> dict[tuple[str, str], dict[str, Any]]:
    """TODOS los pasos ``upload-artifact`` de TODOS los jobs cuya condición incluye ``always()``, por (job, paso)."""
    return {
        (job, str(paso.get("name"))): paso
        for job, definicion in _jobs().items()
        for paso in definicion.get("steps", [])
        if str(paso.get("uses", "")).startswith("actions/upload-artifact@") and "always()" in str(paso.get("if", ""))
    }


def test_las_subidas_que_corren_aun_si_el_job_falla_estan_censadas() -> None:
    """Censo congelado por igualdad: una subida nueva con ``always()`` rompe esto hasta que se decide si necesita
    ``overwrite`` (ver el test siguiente). Que el censo no sea vacío evita que la regla de abajo pase sin mirar nada."""
    assert set(_subidas_que_corren_aun_si_el_job_falla()) == {
        ("test-shard", "Upload shard results"),
        ("test", "Upload coverage"),
    }


def test_una_subida_que_corre_aun_si_el_job_falla_no_bloquea_su_propio_reintento() -> None:
    """Con ``if: always()`` el artefacto queda subido aunque el job falle. ``upload-artifact`` falla por defecto si el
    nombre ya existe en la corrida (``overwrite: false``), así que el reintento («Re-run failed jobs») del mismo shard
    subiría el mismo nombre, chocaría con el de su primer intento y quedaría rojo aunque pytest pase. ``overwrite: true``
    reemplaza el anterior y el patrón de descarga sigue viendo UN artefacto por shard.

    Un sufijo con ``github.run_attempt`` en el nombre NO sirve: el patrón de descarga recogería el artefacto del intento
    fallido y el del reintento, y el verificador recibiría un shard repetido.
    """
    sin_overwrite = [
        f"{job}: {nombre}"
        for (job, nombre), paso in _subidas_que_corren_aun_si_el_job_falla().items()
        if paso.get("with", {}).get("overwrite") is not True
    ]

    assert not sin_overwrite, f"subidas con always() sin overwrite: true (su reintento chocaría): {sin_overwrite}"
    for (_, nombre), paso in _subidas_que_corren_aun_si_el_job_falla().items():
        assert "run_attempt" not in str(paso["with"]["name"]), (
            f"{nombre}: el nombre no puede depender del intento (el verificador vería shards repetidos)"
        )


def test_la_cantidad_de_shards_es_la_misma_en_la_matriz_la_variable_y_el_verificador() -> None:
    """Un shard de menos en la matriz (o un ``--total`` desactualizado) dejaría tests sin correr sin que nada falle."""
    jobs = _jobs()
    en_matriz = jobs["test-shard"]["strategy"]["matrix"]["shard"]
    variable = _paso_por_nombre(jobs["test-shard"], "Run test shard")["env"]["SKYCLAW_SHARD"]
    verificador = _lineas_de_run(_paso_por_nombre(jobs["test"], "Verify shard completeness"))[0]

    assert variable.endswith(f"/{len(en_matriz)}")
    assert jobs["test-shard"]["name"] == (
        f"🧪 Shard ${{{{ matrix.shard }}}}/{len(en_matriz)} / py${{{{ matrix.python-version }}}}"
    )
    assert en_matriz == list(range(1, len(en_matriz) + 1))
    assert re.search(rf"--total {len(en_matriz)}(?:\s|$)", verificador), verificador
    assert len(en_matriz) == TOTAL_DE_SHARDS


def test_toda_accion_de_ci_esta_fijada_por_sha_completo() -> None:
    """Enumera TODA acción de TODO job (incluido el ``download-artifact`` nuevo): ninguna por rama ni por tag móvil."""
    fijada = re.compile(r"^[\w.-]+/[\w.-]+@[0-9a-f]{40}$")
    sin_fijar = [
        f"{job}: {paso['uses']}"
        for job, definicion in _jobs().items()
        for paso in definicion.get("steps", [])
        if "uses" in paso and not fijada.match(str(paso["uses"]))
    ]

    assert not sin_fijar, f"acciones sin fijar por SHA completo: {sin_fijar}"


def test_posix_conserva_una_senal_informativa_acotada() -> None:
    posix = _jobs()["test-posix-core"]

    assert posix["runs-on"] == "ubuntu-latest"
    assert posix["continue-on-error"] is True

    setup_python = _paso_por_id(posix, "setup_python_posix")
    assert setup_python["with"]["python-version"] == "3.11"

    ejecutar = _paso_por_id(posix, "run_posix_tests")
    tokens = shlex.split(ejecutar["run"].replace("\\\n", " "))
    assert tokens[:3] == ["python", "-m", "pytest"]

    objetivos_configurados = sorted(token for token in tokens if token.startswith("tests/"))
    assert objetivos_configurados == sorted(OBJETIVOS_POSIX)


def test_objetivos_posix_apuntan_a_tests_reales_y_cubren_skip_win32_de_etapa6() -> None:
    """Como ``test-posix-core`` lleva ``continue-on-error: true``, la suite obligatoria de
    Windows debe verificar por AST que (a) cada objetivo en ``ci.yml`` existe de verdad y
    (b) todo test de ``test_etapa6_testigo_durable.py`` saltado en ``win32`` (p. ej. defensa
    FIFO ``O_NONBLOCK`` + ``fstat`` y ``fsync`` de directorio padre) se ejecuta en Linux."""
    posix = _jobs()["test-posix-core"]
    ejecutar = _paso_por_id(posix, "run_posix_tests")
    tokens = [t for t in shlex.split(ejecutar["run"].replace("\\\n", " ")) if t.startswith("tests/")]

    modulos_completos: set[str] = set()
    nodos_puntuales: set[tuple[str, str]] = set()
    for objetivo in tokens:
        ruta_rel, sep, nodo = objetivo.partition("::")
        ruta_abs = RAIZ / ruta_rel
        assert ruta_abs.is_file(), f"objetivo POSIX inexistente en ci.yml: {objetivo}"
        if not sep:
            modulos_completos.add(ruta_rel)
            continue
        arbol = ast.parse(ruta_abs.read_text(encoding="utf-8"))
        definidas = {n.name for n in arbol.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        assert nodo in definidas, f"función POSIX inexistente en {ruta_rel}: {nodo}"
        nodos_puntuales.add((ruta_rel, nodo))

    rel_etapa6 = "tests/test_etapa6_testigo_durable.py"
    arbol_etapa6 = ast.parse((RAIZ / rel_etapa6).read_text(encoding="utf-8"))
    exclusivos_posix = [
        n.name
        for n in arbol_etapa6.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        and any("win32" in ast.unparse(dec) and "skipif" in ast.unparse(dec) for dec in n.decorator_list)
    ]
    assert "test_el_almacen_rechaza_archivos_especiales_fifo_sin_bloquear_en_posix" in exclusivos_posix
    for nombre_fn in exclusivos_posix:
        cubierto = rel_etapa6 in modulos_completos or (rel_etapa6, nombre_fn) in nodos_puntuales
        assert cubierto, f"{rel_etapa6}::{nombre_fn} se salta en Windows y tampoco corre en test-posix-core"


def test_concurrencia_ci_aisla_commits_de_main_y_cancela_solo_en_pull_request() -> None:
    """Verifica el contrato de concurrencia de ci.yml:
    - En pull_request: agrupa por ref de PR y cancela corridas obsoletas (ahorro de runners).
    - En push (main/master): agrupa por commit SHA y NO cancela en progreso, evitando
      que merges sucesivos a main aborten la certificación y fallen el fan-in de tests.
    """
    concurrency = _concurrency()

    expresion_grupo_esperada = (
        "${{ github.workflow }}-${{ github.event_name == 'pull_request' && github.ref || github.sha }}"
    )
    expresion_cancel_esperada = "${{ github.event_name == 'pull_request' }}"

    assert concurrency["group"] == expresion_grupo_esperada
    assert concurrency["cancel-in-progress"] == expresion_cancel_esperada


def test_semantica_concurrencia_para_los_tres_escenarios() -> None:
    """Verifica estáticamente la semántica de GitHub Actions para los tres escenarios:
    1. Dos commits sucesivos a main: tienen distinto SHA -> grupos distintos, sin cancelación.
    2. Dos actualizaciones al mismo PR: mismo ref (refs/pull/N/merge) -> mismo grupo, se cancela la anterior.
    3. Re-run del mismo SHA en main: mismo SHA -> mismo grupo, cancel-in-progress=false. No cancela la corrida
       en progreso (aunque re-runs pendientes concurrentes puedan reemplazarse por la cola de GitHub Actions).
    """
    concurrency = _concurrency()
    assert "github.event_name == 'pull_request' && github.ref || github.sha" in concurrency["group"]
    assert concurrency["cancel-in-progress"] == "${{ github.event_name == 'pull_request' }}"

    def evaluar_grupo(event_name: str, ref: str, sha: str, workflow: str = "Sky-Claw CI/CD") -> str:
        # En GitHub Actions: event_name == 'pull_request' && ref || sha
        token = ref if event_name == "pull_request" else sha
        return f"{workflow}-{token}"

    def evaluar_cancel(event_name: str) -> bool:
        # En GitHub Actions: event_name == 'pull_request'
        return event_name == "pull_request"

    # Escenario 1: Dos commits sucesivos a main
    grupo_main_1 = evaluar_grupo("push", "refs/heads/main", "5556d282")
    grupo_main_2 = evaluar_grupo("push", "refs/heads/main", "c30fff4f")
    assert grupo_main_1 != grupo_main_2, "Los commits de main deben tener grupos de concurrencia aislados"
    assert evaluar_cancel("push") is False, "En push a main cancel-in-progress debe ser False"

    # Escenario 2: Dos actualizaciones del mismo PR
    grupo_pr_v1 = evaluar_grupo("pull_request", "refs/pull/898/merge", "11111111")
    grupo_pr_v2 = evaluar_grupo("pull_request", "refs/pull/898/merge", "22222222")
    assert grupo_pr_v1 == grupo_pr_v2, "Las actualizaciones del mismo PR deben compartir grupo para cancelarse"
    assert evaluar_cancel("pull_request") is True, "En pull_request cancel-in-progress debe ser True"

    # Escenario 3: Re-run del mismo SHA
    grupo_rerun_1 = evaluar_grupo("push", "refs/heads/main", "5556d282")
    grupo_rerun_2 = evaluar_grupo("push", "refs/heads/main", "5556d282")
    assert grupo_rerun_1 == grupo_rerun_2
    assert evaluar_cancel("push") is False, (
        "El re-run del mismo SHA en main no cancela la corrida en curso (cancel-in-progress=false)"
    )


# ---------------------------------------------------------------------------
# Capa posterior al merge: corrida nocturna y alerta de main roto
# ---------------------------------------------------------------------------

#: 07:00 UTC = 04:00 en Argentina: la corrida completa de la noche no compite con las de día por los 20 slots.
CRON_NOCTURNO = "0 7 * * *"
ALERTA = "alerta-main"
CONDICION_DE_ALERTA = "${{ failure() && github.event_name != 'pull_request' && github.ref == 'refs/heads/main' }}"
#: Jobs que por política no bloquean (``continue-on-error``): su rojo no es «main roto».
JOBS_INFORMATIVOS = {"test-posix-core"}


def _disparadores() -> dict[str, Any]:
    """La sección ``on`` de ``ci.yml``. PyYAML lee la clave ``on`` como el booleano ``True`` (YAML 1.1)."""
    workflow = yaml.safe_load(WORKFLOW_CI.read_text(encoding="utf-8"))
    return cast(dict[str, Any], workflow.get("on", workflow.get(True)))


def _ancestros(jobs: dict[str, Any], nombre: str) -> set[str]:
    """Todos los jobs de los que ``nombre`` depende, directa o transitivamente (``failure()`` mira a TODOS)."""
    pendientes = [nombre]
    vistos: set[str] = set()
    while pendientes:
        needs = jobs[pendientes.pop()].get("needs", [])
        for padre in [needs] if isinstance(needs, str) else needs:
            if padre not in vistos:
                vistos.add(padre)
                pendientes.append(padre)
    return vistos


def test_el_workflow_corre_cada_noche_y_a_pedido_ademas_de_push_y_pr() -> None:
    """La corrida nocturna completa (las dos versiones de Python) detecta lo que ningún commit cambió: una dependencia
    nueva que ``pip`` resuelve distinto (como el ruff 0.17.4 de CI-FIX-003) o un test inestable. ``workflow_dispatch``
    permite correr la suite completa, py3.12 incluido, sobre una rama antes de mergearla."""
    disparadores = _disparadores()

    assert set(disparadores) == {"push", "pull_request", "schedule", "workflow_dispatch"}
    assert disparadores["schedule"] == [{"cron": CRON_NOCTURNO}]
    assert disparadores["push"] == {"branches": ["main", "master"]}


def test_la_alerta_depende_de_todo_job_que_no_sea_informativo() -> None:
    """Enumera los jobs REALES: uno nuevo que no cuelgue (directa o transitivamente) de la alerta rompe esto, porque su
    rojo no avisaría a nadie."""
    jobs = _jobs()

    assert _ancestros(jobs, ALERTA) == set(jobs) - {ALERTA} - JOBS_INFORMATIVOS
    assert all(jobs[j].get("continue-on-error") is True for j in JOBS_INFORMATIVOS)
    assert not [
        n
        for n, j in jobs.items()
        if ALERTA in ([j["needs"]] if isinstance(j.get("needs"), str) else j.get("needs", []))
    ]


def test_la_alerta_solo_corre_si_algo_fallo_fuera_de_un_pr_y_sobre_main() -> None:
    """En un PR el rojo ya lo ve quien lo abrió; un ``workflow_dispatch`` sobre otra rama no es «main roto»."""
    alerta = _jobs()[ALERTA]

    assert alerta["if"] == CONDICION_DE_ALERTA
    assert alerta["runs-on"] == "ubuntu-latest"
    assert "continue-on-error" not in alerta


def test_la_alerta_es_el_unico_job_que_puede_escribir_y_solo_sobre_issues() -> None:
    """Mínimo privilegio, enumerado: el workflow sigue en ``contents: read`` y ningún otro job gana permisos."""
    workflow = yaml.safe_load(WORKFLOW_CI.read_text(encoding="utf-8"))
    con_permisos_propios = {nombre: job["permissions"] for nombre, job in _jobs().items() if "permissions" in job}

    assert workflow["permissions"] == {"contents": "read"}
    assert con_permisos_propios == {ALERTA: {"contents": "read", "issues": "write"}}


def test_la_alerta_no_interpola_expresiones_en_el_script_ni_deja_credenciales_en_el_checkout() -> None:
    """Los datos de la corrida entran por ``env``, nunca por ``${{ }}`` dentro de ``run`` (inyección de comandos)."""
    pasos = _jobs()[ALERTA]["steps"]

    assert all("${{" not in str(paso.get("run", "")) for paso in pasos)
    checkouts = [p for p in pasos if str(p.get("uses", "")).startswith("actions/checkout@")]
    assert len(checkouts) == 1
    assert checkouts[0]["with"] == {"persist-credentials": False}

"""Anclas del gate «Main en verde» (``scripts/ci_main_en_verde.py`` y ``.github/workflows/main-en-verde.yml``).

La alerta de ``main`` roto abre un issue, pero un issue no frena a nadie: el PR siguiente se mergea igual sobre un
``main`` en rojo. El gate es un check que falla mientras la última corrida TERMINADA de CI sobre ``main`` tenga en rojo
alguno de los checks obligatorios, y que se vuelve a evaluar en cada PR abierto cuando cambia el estado de ``main``: un
check que se evaluara una sola vez, al abrir el PR, quedaría en verde aunque ``main`` se rompiera una hora después.

El script habla con GitHub por un ejecutor inyectable: acá se ejerce con uno falso que responde por subcomando, así que
cada rama (verde / rojo / sin veredicto / etiqueta de salida / re-evaluación) se prueba sin red y en cualquier plataforma.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from scripts import ci_main_en_verde as gate

RAIZ = Path(__file__).resolve().parents[1]
WORKFLOW_CI = RAIZ / ".github" / "workflows" / "ci.yml"
WORKFLOW_GATE = RAIZ / ".github" / "workflows" / gate.WORKFLOW_DEL_GATE

OBLIGATORIOS = ["🔍 Lint (Ruff)", "🏷️ Type Check (Mypy)", "🧪 Tests (Pytest)", "🛡️ Security Scan"]


def _corrida(numero: int, evento: str = "push") -> dict[str, Any]:
    return {
        "databaseId": numero,
        "event": evento,
        "headSha": f"{numero:040x}",
        "url": f"https://github.com/dueño/repo/actions/runs/{numero}",
    }


def _jobs(conclusiones: dict[str, str]) -> list[dict[str, Any]]:
    """Los jobs de una corrida: los obligatorios en verde salvo lo que diga ``conclusiones``."""
    base = {nombre: "success" for nombre in OBLIGATORIOS}
    base.update(conclusiones)
    return [
        {"name": nombre, "conclusion": conclusion, "databaseId": 1000 + i}
        for i, (nombre, conclusion) in enumerate(base.items())
    ]


class GhFalso:
    """Responde ``gh`` por subcomando con datos fijos y registra cada llamada."""

    def __init__(
        self,
        corridas_de_main: list[dict[str, Any]] | None = None,
        jobs_por_corrida: dict[int, list[dict[str, Any]]] | None = None,
        etiquetas: list[str] | None = None,
        prs_abiertos: list[dict[str, Any]] | None = None,
        corridas_del_gate: dict[str, list[dict[str, Any]]] | None = None,
        rerun_falla: set[int] | None = None,
        estados_sucesivos: dict[int, list[str]] | None = None,
    ) -> None:
        self.corridas_de_main = corridas_de_main or []
        self.jobs_por_corrida = jobs_por_corrida or {}
        self.etiquetas = etiquetas or []
        self.prs_abiertos = prs_abiertos or []
        self.corridas_del_gate = corridas_del_gate or {}
        self.rerun_falla = rerun_falla or set()
        #: Lo que va devolviendo ``gh run view N --json status`` en cada consulta (una corrida que termina mientras se espera).
        self.estados_sucesivos = estados_sucesivos or {}
        self.llamadas: list[list[str]] = []

    def __call__(self, argumentos: Sequence[str]) -> str:
        args = list(argumentos)
        self.llamadas.append(args)
        if args[:2] == ["pr", "view"]:
            return json.dumps({"labels": [{"name": e} for e in self.etiquetas]})
        if args[:2] == ["pr", "list"]:
            return json.dumps(self.prs_abiertos)
        if args[:2] == ["run", "list"] and gate.WORKFLOW_DE_CI in args:
            return json.dumps(self.corridas_de_main)
        if args[:2] == ["run", "list"] and gate.WORKFLOW_DEL_GATE in args:
            return json.dumps(self.corridas_del_gate.get(args[args.index("--commit") + 1], []))
        if args[:2] == ["run", "view"] and args[-2:] == ["--json", "status"]:
            estados = self.estados_sucesivos[int(args[2])]
            return json.dumps({"status": estados.pop(0) if len(estados) > 1 else estados[0]})
        if args[:2] == ["run", "view"]:
            return json.dumps({"jobs": self.jobs_por_corrida.get(int(args[2]), [])})
        if args[:2] == ["run", "rerun"]:
            if int(args[2]) in self.rerun_falla:
                raise subprocess.CalledProcessError(1, ["gh", *args], stderr="gh run rerun falló")
            return ""
        raise AssertionError(f"llamada a gh no prevista: {args}")

    def reruns(self) -> list[list[str]]:
        return [llamada for llamada in self.llamadas if llamada[:2] == ["run", "rerun"]]


# ── Evaluación ────────────────────────────────────────────────────────


def test_main_en_verde_deja_pasar_el_pr() -> None:
    gh = GhFalso([_corrida(7)], {7: _jobs({})})

    veredicto = gate.evaluar(12, gh)

    assert veredicto.verde
    assert "actions/runs/7" in veredicto.mensaje


def test_un_obligatorio_en_rojo_bloquea_y_nombra_el_check_y_la_corrida() -> None:
    gh = GhFalso([_corrida(7)], {7: _jobs({"🧪 Tests (Pytest)": "failure"})})

    veredicto = gate.evaluar(12, gh)

    assert not veredicto.verde
    assert "🧪 Tests (Pytest)" in veredicto.mensaje
    assert "actions/runs/7" in veredicto.mensaje


def test_un_obligatorio_que_agoto_su_tiempo_cuenta_como_rojo() -> None:
    gh = GhFalso([_corrida(7)], {7: _jobs({"🛡️ Security Scan": "timed_out"})})

    assert not gate.evaluar(12, gh).verde


def test_un_job_que_no_es_obligatorio_en_rojo_no_bloquea() -> None:
    """El build y el POSIX informativo no son checks obligatorios: su rojo lo avisa la alerta, no frena merges."""
    jobs = [*_jobs({}), {"name": "📦 Build", "conclusion": "failure", "databaseId": 1}]
    gh = GhFalso([_corrida(7)], {7: jobs})

    assert gate.evaluar(12, gh).verde


def test_consulta_solo_corridas_terminadas_de_ci_sobre_main() -> None:
    gh = GhFalso([_corrida(7)], {7: _jobs({})})

    gate.evaluar(12, gh)

    (consulta,) = [llamada for llamada in gh.llamadas if llamada[:2] == ["run", "list"]]
    assert consulta[consulta.index("--workflow") + 1] == gate.WORKFLOW_DE_CI
    assert consulta[consulta.index("--branch") + 1] == "main"
    assert consulta[consulta.index("--status") + 1] == "completed"


@pytest.mark.parametrize("evento", ["pull_request", "pull_request_target", "workflow_run"])
def test_una_corrida_que_no_es_de_main_no_cuenta_aunque_su_rama_se_llame_main(evento: str) -> None:
    """Un PR desde un fork cuya rama se llama ``main`` aparece con ``--branch main``: su rojo no es «main roto»."""
    gh = GhFalso(
        [_corrida(9, evento), _corrida(7)],
        {9: _jobs({"🧪 Tests (Pytest)": "failure"}), 7: _jobs({})},
    )

    veredicto = gate.evaluar(12, gh)

    assert veredicto.verde
    assert "actions/runs/7" in veredicto.mensaje


@pytest.mark.parametrize("evento", sorted(gate.EVENTOS_DE_MAIN))
def test_cuentan_el_push_la_nocturna_y_la_corrida_a_pedido(evento: str) -> None:
    gh = GhFalso([_corrida(7, evento)], {7: _jobs({"🔍 Lint (Ruff)": "failure"})})

    assert not gate.evaluar(12, gh).verde


@pytest.mark.parametrize("conclusion", ["cancelled", "skipped", "neutral", "", None])
def test_una_corrida_sin_veredicto_se_saltea_y_decide_la_anterior(conclusion: str | None) -> None:
    """Una corrida cancelada no dice si ``main`` está roto: decide la anterior que sí terminó con veredicto."""
    gh = GhFalso(
        [_corrida(9), _corrida(7)],
        {9: _jobs({"🧪 Tests (Pytest)": cast(str, conclusion)}), 7: _jobs({"🧪 Tests (Pytest)": "failure"})},
    )

    veredicto = gate.evaluar(12, gh)

    assert not veredicto.verde
    assert "actions/runs/7" in veredicto.mensaje


def test_una_corrida_a_la_que_le_falta_un_obligatorio_no_tiene_veredicto() -> None:
    sin_seguridad = [j for j in _jobs({}) if j["name"] != "🛡️ Security Scan"]
    gh = GhFalso([_corrida(9), _corrida(7)], {9: sin_seguridad, 7: _jobs({"🛡️ Security Scan": "failure"})})

    assert not gate.evaluar(12, gh).verde


def test_un_rojo_gana_aunque_otro_obligatorio_de_la_misma_corrida_no_tenga_veredicto() -> None:
    gh = GhFalso([_corrida(7)], {7: _jobs({"🔍 Lint (Ruff)": "cancelled", "🧪 Tests (Pytest)": "failure"})})

    assert not gate.evaluar(12, gh).verde


def test_sin_ninguna_corrida_con_veredicto_falla_cerrado() -> None:
    """No saber el estado de ``main`` no es «está en verde»: el gate bloquea y la salida es la etiqueta."""
    gh = GhFalso([_corrida(9)], {9: _jobs({"🧪 Tests (Pytest)": "cancelled"})})

    veredicto = gate.evaluar(12, gh)

    assert not veredicto.verde
    assert gate.ETIQUETA_DE_SALIDA in veredicto.mensaje


def test_sin_corridas_de_main_falla_cerrado() -> None:
    assert not gate.evaluar(12, GhFalso()).verde


def test_la_etiqueta_de_salida_deja_pasar_el_pr_que_arregla_main() -> None:
    gh = GhFalso([_corrida(7)], {7: _jobs({"🧪 Tests (Pytest)": "failure"})}, etiquetas=[gate.ETIQUETA_DE_SALIDA])

    veredicto = gate.evaluar(12, gh)

    assert veredicto.verde
    assert gate.ETIQUETA_DE_SALIDA in veredicto.mensaje
    # Igual informa el estado real de main, para que quede en el log por qué se usó la salida.
    assert "🧪 Tests (Pytest)" in veredicto.mensaje


def test_las_etiquetas_se_leen_en_vivo_del_pr_y_no_del_evento() -> None:
    """Al re-correr el job, el payload del evento es el ORIGINAL: una etiqueta puesta después solo se ve consultándola."""
    gh = GhFalso([_corrida(7)], {7: _jobs({})})

    gate.evaluar(12, gh)

    assert ["pr", "view", "12", "--json", "labels"] in gh.llamadas


def test_una_etiqueta_parecida_no_es_la_salida() -> None:
    gh = GhFalso([_corrida(7)], {7: _jobs({"🧪 Tests (Pytest)": "failure"})}, etiquetas=["ci:repara-main-no"])

    assert not gate.evaluar(12, gh).verde


@pytest.mark.parametrize(
    "respuesta",
    ["no es json", json.dumps({"no": "es una lista"}), json.dumps([{"databaseId": "7"}])],
)
def test_una_respuesta_de_gh_que_no_se_entiende_es_un_error(respuesta: str) -> None:
    """Una forma inesperada no puede leerse como «no hay corridas en rojo»."""

    def gh(argumentos: Sequence[str]) -> str:
        if list(argumentos[:2]) == ["pr", "view"]:
            return json.dumps({"labels": []})
        return respuesta

    with pytest.raises(ValueError, match="gh"):
        gate.evaluar(12, gh)


# ── Re-evaluación ──────────────────────────────────────────────────────


def _corrida_del_gate(numero: int, estado: str = "completed", evento: str = "pull_request") -> dict[str, Any]:
    return {"databaseId": numero, "status": estado, "event": evento}


def _job_del_gate(numero: int) -> list[dict[str, Any]]:
    return [{"name": gate.NOMBRE_DEL_GATE, "conclusion": "success", "databaseId": numero}]


def test_reevaluar_vuelve_a_correr_el_gate_de_cada_pr_abierto() -> None:
    gh = GhFalso(
        prs_abiertos=[{"number": 1, "headRefOid": "a" * 40}, {"number": 2, "headRefOid": "b" * 40}],
        corridas_del_gate={"a" * 40: [_corrida_del_gate(51)], "b" * 40: [_corrida_del_gate(52)]},
        jobs_por_corrida={51: _job_del_gate(501), 52: _job_del_gate(502)},
    )

    assert gate.reevaluar(gh) == 0
    assert gh.reruns() == [["run", "rerun", "51", "--job", "501"], ["run", "rerun", "52", "--job", "502"]]


def test_reevaluar_lista_solo_los_prs_abiertos_contra_main() -> None:
    gh = GhFalso()

    gate.reevaluar(gh)

    (consulta,) = [llamada for llamada in gh.llamadas if llamada[:2] == ["pr", "list"]]
    assert consulta[consulta.index("--base") + 1] == "main"
    assert consulta[consulta.index("--state") + 1] == "open"


def test_reevaluar_usa_la_corrida_mas_nueva_del_gate_para_ese_commit() -> None:
    gh = GhFalso(
        prs_abiertos=[{"number": 1, "headRefOid": "a" * 40}],
        corridas_del_gate={"a" * 40: [_corrida_del_gate(60), _corrida_del_gate(51)]},
        jobs_por_corrida={60: _job_del_gate(600), 51: _job_del_gate(501)},
    )

    gate.reevaluar(gh)

    assert gh.reruns() == [["run", "rerun", "60", "--job", "600"]]


class EsperaFalsa:
    """Registra cada pausa sin dormir."""

    def __init__(self) -> None:
        self.pausas: list[float] = []

    def __call__(self, segundos: float) -> None:
        self.pausas.append(segundos)


def test_reevaluar_espera_a_que_termine_un_gate_en_curso_y_despues_lo_re_corre() -> None:
    """Un gate en curso pudo haber leído las corridas de main ANTES de que terminara la que disparó esta reevaluación:
    saltearlo dejaría su veredicto viejo fijo hasta el próximo evento del PR. Se espera a que termine y se re-corre."""
    gh = GhFalso(
        prs_abiertos=[{"number": 1, "headRefOid": "a" * 40}],
        corridas_del_gate={"a" * 40: [_corrida_del_gate(51, estado="in_progress")]},
        jobs_por_corrida={51: _job_del_gate(501)},
        estados_sucesivos={51: ["in_progress", "queued", "completed"]},
    )
    espera = EsperaFalsa()

    assert gate.reevaluar(gh, espera) == 0
    assert gh.reruns() == [["run", "rerun", "51", "--job", "501"]]
    assert len(espera.pausas) == 3


def test_reevaluar_falla_si_el_gate_en_curso_no_termina_a_tiempo() -> None:
    """Si nunca termina, no se puede garantizar que lea el estado nuevo: el job queda rojo, a la vista."""
    gh = GhFalso(
        prs_abiertos=[{"number": 1, "headRefOid": "a" * 40}],
        corridas_del_gate={"a" * 40: [_corrida_del_gate(51, estado="in_progress")]},
        jobs_por_corrida={51: _job_del_gate(501)},
        estados_sucesivos={51: ["in_progress"]},
    )
    espera = EsperaFalsa()

    assert gate.reevaluar(gh, espera) == 1
    assert gh.reruns() == []
    assert len(espera.pausas) == gate.INTENTOS_DE_ESPERA
    # La espera total entra en el timeout del job ``reevaluar`` (10 min) con margen.
    assert gate.INTENTOS_DE_ESPERA * gate.PAUSA_DE_ESPERA <= 5 * 60


def test_reevaluar_ignora_corridas_del_gate_que_no_son_de_un_pr() -> None:
    gh = GhFalso(
        prs_abiertos=[{"number": 1, "headRefOid": "a" * 40}],
        corridas_del_gate={"a" * 40: [_corrida_del_gate(70, evento="workflow_dispatch"), _corrida_del_gate(51)]},
        jobs_por_corrida={51: _job_del_gate(501)},
    )

    gate.reevaluar(gh)

    assert gh.reruns() == [["run", "rerun", "51", "--job", "501"]]


def test_reevaluar_sin_corrida_del_gate_no_falla() -> None:
    """Un PR abierto antes de que existiera el gate no tiene corrida: su check falta y eso ya bloquea a la vista."""
    gh = GhFalso(prs_abiertos=[{"number": 1, "headRefOid": "a" * 40}])

    assert gate.reevaluar(gh) == 0
    assert gh.reruns() == []


def test_reevaluar_sigue_con_los_demas_si_uno_falla_y_termina_en_rojo() -> None:
    """Un PR que no se pudo re-evaluar puede quedar en verde con main roto: el job tiene que quedar rojo."""
    gh = GhFalso(
        prs_abiertos=[{"number": 1, "headRefOid": "a" * 40}, {"number": 2, "headRefOid": "b" * 40}],
        corridas_del_gate={"a" * 40: [_corrida_del_gate(51)], "b" * 40: [_corrida_del_gate(52)]},
        jobs_por_corrida={51: _job_del_gate(501), 52: _job_del_gate(502)},
        rerun_falla={51},
    )

    assert gate.reevaluar(gh) == 1
    assert ["run", "rerun", "52", "--job", "502"] in gh.reruns()


def test_reevaluar_falla_si_la_corrida_no_tiene_el_job_del_gate() -> None:
    gh = GhFalso(
        prs_abiertos=[{"number": 1, "headRefOid": "a" * 40}],
        corridas_del_gate={"a" * 40: [_corrida_del_gate(51)]},
        jobs_por_corrida={51: []},
    )

    assert gate.reevaluar(gh) == 1


# ── Punto de entrada ───────────────────────────────────────────────────


def test_evaluar_sin_el_numero_de_pr_sale_con_error_de_uso() -> None:
    assert gate.main(["evaluar"], {}, GhFalso()) == 2


def test_evaluar_devuelve_1_con_main_en_rojo_y_0_en_verde() -> None:
    rojo = GhFalso([_corrida(7)], {7: _jobs({"🧪 Tests (Pytest)": "failure"})})
    verde = GhFalso([_corrida(7)], {7: _jobs({})})

    assert gate.main(["evaluar"], {"PR": "12"}, rojo) == 1
    assert gate.main(["evaluar"], {"PR": "12"}, verde) == 0


def test_un_subcomando_desconocido_sale_con_error_de_uso() -> None:
    assert gate.main(["otro"], {}, GhFalso()) == 2


# ── Anclas contra el YAML real ─────────────────────────────────────────


def _cargar(ruta: Path) -> dict[str, Any]:
    return cast(dict[str, Any], yaml.safe_load(ruta.read_text(encoding="utf-8")))


def _disparadores(workflow: dict[str, Any]) -> dict[str, Any]:
    """PyYAML lee la clave ``on`` como el booleano ``True`` (YAML 1.1)."""
    return cast(dict[str, Any], workflow.get("on", workflow.get(True)))


def test_los_obligatorios_del_gate_son_los_nombres_de_los_jobs_que_publica_ci() -> None:
    """Igualdad literal con los cuatro checks que exige la protección de ``main``, y cada uno existe en ``ci.yml``: si
    un job cambia de nombre, el gate no puede quedar mirando un check que ya no existe (eso sería «sin veredicto»)."""
    nombres_en_ci = [job["name"] for job in _cargar(WORKFLOW_CI)["jobs"].values()]

    assert list(gate.CHECKS_OBLIGATORIOS) == OBLIGATORIOS
    assert all(nombres_en_ci.count(nombre) == 1 for nombre in gate.CHECKS_OBLIGATORIOS)


def test_los_eventos_de_main_son_todos_los_disparadores_de_ci_salvo_el_pr() -> None:
    """Si ``ci.yml`` gana un disparador nuevo, el gate tiene que decidir si cuenta: esto lo obliga."""
    disparadores_de_main = frozenset(_disparadores(_cargar(WORKFLOW_CI))) - {"pull_request"}

    assert disparadores_de_main == gate.EVENTOS_DE_MAIN


def test_el_workflow_de_ci_del_gate_es_el_archivo_real() -> None:
    assert (WORKFLOW_CI.parent / gate.WORKFLOW_DE_CI).resolve() == WORKFLOW_CI.resolve()


def test_el_gate_corre_en_cada_cambio_del_pr_y_al_terminar_ci_sobre_main() -> None:
    """``labeled``/``unlabeled``: poner o sacar la etiqueta de salida re-evalúa en el acto. ``workflow_run``: cada vez
    que termina una corrida de CI sobre main se re-evalúan los PRs abiertos. Ningún otro disparador."""
    workflow = _cargar(WORKFLOW_GATE)
    nombre_de_ci = _cargar(WORKFLOW_CI)["name"]

    assert _disparadores(workflow) == {
        "pull_request": {
            "branches": ["main"],
            "types": ["opened", "synchronize", "reopened", "labeled", "unlabeled"],
        },
        "workflow_run": {"workflows": [nombre_de_ci], "types": ["completed"], "branches": ["main"]},
        "workflow_dispatch": None,
    }


def test_el_check_se_llama_como_el_que_exigira_la_proteccion_y_solo_corre_en_prs() -> None:
    jobs = _cargar(WORKFLOW_GATE)["jobs"]

    assert set(jobs) == {"gate", "reevaluar"}
    assert jobs["gate"]["name"] == gate.NOMBRE_DEL_GATE == "🚦 Main en verde"
    assert jobs["gate"]["if"] == "${{ github.event_name == 'pull_request' }}"
    assert jobs["reevaluar"]["if"] == (
        "${{ github.event_name == 'workflow_dispatch' || "
        "(github.event_name == 'workflow_run' && github.event.workflow_run.event != 'pull_request') }}"
    )
    # Aislado: sin needs y sin dependientes, re-correrlo no arrastra a ningún otro job.
    assert all("needs" not in job for job in jobs.values())


def test_permisos_minimos_enumerados_por_job() -> None:
    workflow = _cargar(WORKFLOW_GATE)

    assert workflow["permissions"] == {"contents": "read"}
    assert {nombre: job.get("permissions") for nombre, job in workflow["jobs"].items()} == {
        "gate": {"contents": "read", "actions": "read", "pull-requests": "read"},
        "reevaluar": {"contents": "read", "actions": "write", "pull-requests": "read"},
    }


def test_la_concurrencia_agrupa_por_pr_y_deja_una_sola_reevaluacion() -> None:
    """Por PR: un evento nuevo cancela la evaluación vieja. Reevaluación: la más nueva cancela a la anterior (re-corre
    TODOS los PRs con el estado más reciente). Un ``workflow_run`` de un PR desde un fork cuya rama se llama ``main``
    no hace nada y va a su propio grupo: en el de ``reevaluar`` cancelaría una reevaluación real."""
    assert _cargar(WORKFLOW_GATE)["concurrency"] == {
        "group": (
            "${{ github.workflow }}-${{ github.event.pull_request.number || "
            "(github.event.workflow_run.event == 'pull_request' && github.run_id) || 'reevaluar' }}"
        ),
        "cancel-in-progress": True,
    }


def test_toda_accion_del_gate_esta_fijada_por_sha_y_sin_credenciales_persistidas() -> None:
    fijada = re.compile(r"^[\w.-]+/[\w.-]+@[0-9a-f]{40}$")
    pasos = [paso for job in _cargar(WORKFLOW_GATE)["jobs"].values() for paso in job["steps"]]
    acciones = [paso for paso in pasos if "uses" in paso]

    assert acciones and all(fijada.match(str(paso["uses"])) for paso in acciones)
    assert all(paso["with"] == {"persist-credentials": False} for paso in acciones)


def test_el_gate_no_interpola_expresiones_en_run_y_le_pasa_lo_que_el_script_lee() -> None:
    """Los datos entran por ``env`` (nunca ``${{ }}`` dentro de ``run``) y son exactamente los que el script lee."""
    jobs = _cargar(WORKFLOW_GATE)["jobs"]
    pasos_con_run = {nombre: [p for p in job["steps"] if "run" in p] for nombre, job in jobs.items()}

    assert all("${{" not in str(p["run"]) for pasos in pasos_con_run.values() for p in pasos)
    ((paso_gate,), (paso_reevaluar,)) = pasos_con_run["gate"], pasos_con_run["reevaluar"]
    assert paso_gate["run"] == "python3 scripts/ci_main_en_verde.py evaluar"
    assert paso_reevaluar["run"] == "python3 scripts/ci_main_en_verde.py reevaluar"
    assert paso_gate["env"] == {
        "GH_TOKEN": "${{ github.token }}",
        "GH_REPO": "${{ github.repository }}",
        "PR": "${{ github.event.pull_request.number }}",
    }
    assert paso_reevaluar["env"] == {"GH_TOKEN": "${{ github.token }}", "GH_REPO": "${{ github.repository }}"}

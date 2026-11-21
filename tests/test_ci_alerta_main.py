"""Anclas de la alerta de ``main`` roto (``scripts/ci_alerta_main.py`` y el job ``alerta-main`` de ``ci.yml``).

Desde que un PR corre solo py3.11, una regresión exclusiva de py3.12 recién aparece en el push a ``main`` (o en la corrida
nocturna). Si esa señal es solo una cruz roja en un commit, nadie la ve y los PRs siguientes se mergean sobre un ``main``
roto. La alerta la vuelve un issue único y persistente: lo abre la primera falla y lo COMENTA cada falla siguiente.

El script habla con GitHub por un ejecutor inyectable: acá se ejerce con uno falso que registra cada llamada, así que las
ramas (crear / comentar / fallar) se prueban de verdad sin red y en cualquier plataforma.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from scripts import ci_alerta_main

RAIZ = Path(__file__).resolve().parents[1]
WORKFLOW_CI = RAIZ / ".github" / "workflows" / "ci.yml"

ENTORNO_VALIDO = {
    "EVENTO": "push",
    "COMMIT": "0123456789abcdef0123456789abcdef01234567",
    "URL_DE_LA_CORRIDA": "https://github.com/dueño/repo/actions/runs/42",
}


class GhFalso:
    """Ejecutor que registra cada llamada a ``gh`` y responde ``issue list`` con los issues que se le den."""

    def __init__(self, abiertos: list[dict[str, Any]] | None = None) -> None:
        self.abiertos = abiertos or []
        self.llamadas: list[list[str]] = []

    def __call__(self, argumentos: Sequence[str]) -> str:
        self.llamadas.append(list(argumentos))
        if list(argumentos[:2]) == ["issue", "list"]:
            return json.dumps(self.abiertos)
        return ""

    def escrituras(self) -> list[list[str]]:
        return [llamada for llamada in self.llamadas if llamada[:2] != ["issue", "list"]]


def _alertar(gh: GhFalso) -> str:
    return ci_alerta_main.alertar(
        ENTORNO_VALIDO["EVENTO"], ENTORNO_VALIDO["COMMIT"], ENTORNO_VALIDO["URL_DE_LA_CORRIDA"], gh
    )


def test_sin_un_issue_abierto_crea_uno_con_el_titulo_fijo() -> None:
    gh = GhFalso()

    resultado = _alertar(gh)

    assert resultado == "creado"
    (escritura,) = gh.escrituras()
    assert escritura[:4] == ["issue", "create", "--title", ci_alerta_main.TITULO]
    assert escritura[4] == "--body"


def test_con_un_issue_abierto_lo_comenta_en_vez_de_abrir_otro() -> None:
    """Una falla por noche durante una semana es UN issue con siete comentarios, no siete issues."""
    gh = GhFalso([{"number": 17, "title": ci_alerta_main.TITULO}])

    resultado = _alertar(gh)

    assert resultado == "comentado #17"
    (escritura,) = gh.escrituras()
    assert escritura[:3] == ["issue", "comment", "17"]
    assert escritura[3] == "--body"


def test_un_issue_de_titulo_parecido_no_cuenta_como_el_de_la_alerta() -> None:
    """La búsqueda de GitHub es por palabras: «CI de main roto (viejo)» también aparece. Solo vale el título exacto."""
    gh = GhFalso([{"number": 5, "title": f"{ci_alerta_main.TITULO} (viejo)"}, {"number": 6, "title": "main roto"}])

    assert _alertar(gh) == "creado"


def test_con_varios_abiertos_comenta_el_mas_viejo() -> None:
    gh = GhFalso([{"number": 30, "title": ci_alerta_main.TITULO}, {"number": 12, "title": ci_alerta_main.TITULO}])

    assert _alertar(gh) == "comentado #12"


def test_la_busqueda_pide_solo_issues_abiertos_por_titulo() -> None:
    gh = GhFalso()

    _alertar(gh)

    busqueda = gh.llamadas[0]
    assert busqueda[:2] == ["issue", "list"]
    assert busqueda[busqueda.index("--state") + 1] == "open"
    assert ci_alerta_main.TITULO in busqueda[busqueda.index("--search") + 1]
    assert busqueda[busqueda.index("--json") + 1] == "number,title"


def test_el_aviso_nombra_la_corrida_el_evento_y_el_commit() -> None:
    gh = GhFalso()

    _alertar(gh)

    cuerpo = gh.escrituras()[0][-1]
    for valor in ENTORNO_VALIDO.values():
        assert valor in cuerpo


def test_un_fallo_de_gh_no_se_traga_y_deja_el_job_en_rojo() -> None:
    """Si no se pudo avisar, el job de la alerta tiene que fallar: un aviso perdido en silencio es justo lo que se evita."""

    def gh_roto(argumentos: Sequence[str]) -> str:
        raise subprocess.CalledProcessError(1, ["gh", *argumentos])

    with pytest.raises(subprocess.CalledProcessError):
        ci_alerta_main.alertar("push", "abc", "https://x", gh_roto)


@pytest.mark.parametrize("salida", ["no es json", "{}", '[{"title": "CI de main roto"}]'])
def test_una_respuesta_inesperada_de_gh_no_se_interpreta_como_sin_issue(salida: str) -> None:
    """Leer mal la lista y concluir «no hay issue» abriría uno nuevo por corrida: una respuesta rara es un error."""

    def gh_raro(argumentos: Sequence[str]) -> str:
        return salida

    with pytest.raises(ValueError, match="gh issue list"):
        ci_alerta_main.alertar("push", "abc", "https://x", gh_raro)


@pytest.mark.parametrize("faltante", sorted(ENTORNO_VALIDO))
def test_sin_una_variable_del_workflow_sale_con_error_de_uso_y_no_llama_a_gh(
    faltante: str, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = GhFalso()
    entorno = {k: v for k, v in ENTORNO_VALIDO.items() if k != faltante}

    codigo = ci_alerta_main.main(entorno, gh)

    assert codigo == 2
    assert faltante in capsys.readouterr().err
    assert gh.llamadas == []


def test_con_el_entorno_completo_avisa_y_sale_con_cero(capsys: pytest.CaptureFixture[str]) -> None:
    gh = GhFalso()

    assert ci_alerta_main.main(ENTORNO_VALIDO, gh) == 0
    assert "creado" in capsys.readouterr().out


def _paso_de_la_alerta() -> dict[str, Any]:
    workflow = yaml.safe_load(WORKFLOW_CI.read_text(encoding="utf-8"))
    pasos = [p for p in workflow["jobs"]["alerta-main"]["steps"] if "run" in p]
    assert len(pasos) == 1, "la alerta debería tener un solo paso que ejecuta el script"
    return cast(dict[str, Any], pasos[0])


def test_las_variables_que_lee_el_script_son_exactamente_las_que_el_workflow_le_pasa() -> None:
    """Hermanos: el ``env`` del paso y lo que el script exige. Una variable renombrada de un solo lado dejaría la alerta
    saliendo con error de uso justo el día que main se rompe."""
    paso = _paso_de_la_alerta()

    assert paso["run"] == "python3 scripts/ci_alerta_main.py"
    assert set(paso["env"]) == set(ci_alerta_main.VARIABLES_REQUERIDAS) | {"GH_TOKEN", "GH_REPO"}
    assert set(ci_alerta_main.VARIABLES_REQUERIDAS) == set(ENTORNO_VALIDO)

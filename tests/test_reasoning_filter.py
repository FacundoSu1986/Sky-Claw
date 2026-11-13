"""``ocultar_razonamiento``: el bloque ``<thought>`` del modelo no llega al usuario (#918).

El ``SYSTEM_PROMPT`` le pide al modelo razonar dentro de ``<thought>…</thought>``
«oculto al usuario», pero nada lo ocultaba: el texto salía tal cual hacia Telegram
(que lo rechazaba por HTML inválido), la GUI, la web y el CLI. Esta función es la
única que decide qué es razonamiento y qué es respuesta.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from sky_claw.app.agent.reasoning_filter import ocultar_razonamiento

RAIZ_PAQUETE = pathlib.Path(__file__).resolve().parents[1] / "sky_claw"
MODULO_DEL_FILTRO = "app/agent/reasoning_filter.py"
_NOMBRES_DE_FILTRO = {"ocultar_razonamiento", "quitar_razonamiento"}
_FUNCIONES_DE_REGEX = {"compile", "sub", "search", "match", "findall", "finditer", "fullmatch", "split"}


def _modulos_con_filtro_de_razonamiento_propio() -> list[str]:
    """Módulos fuera del filtro que lo reimplementan: una función con su nombre o una regex sobre las etiquetas."""
    infractores: list[str] = []
    for ruta in sorted(RAIZ_PAQUETE.rglob("*.py")):
        relativa = ruta.relative_to(RAIZ_PAQUETE).as_posix()
        if relativa == MODULO_DEL_FILTRO:
            continue
        arbol = ast.parse(ruta.read_text(encoding="utf-8"))
        for nodo in ast.walk(arbol):
            define_el_filtro = (
                isinstance(nodo, ast.FunctionDef | ast.AsyncFunctionDef) and nodo.name in _NOMBRES_DE_FILTRO
            )
            es_regex_de_etiquetas = (
                isinstance(nodo, ast.Call)
                and isinstance(nodo.func, ast.Attribute)
                and nodo.func.attr in _FUNCIONES_DE_REGEX
                and getattr(nodo.func.value, "id", "") == "re"
                and bool(nodo.args)
                and isinstance(nodo.args[0], ast.Constant)
                and isinstance(nodo.args[0].value, str)
                and any(etiqueta in nodo.args[0].value.lower() for etiqueta in ("thought", "think"))
            )
            if define_el_filtro or es_regex_de_etiquetas:
                infractores.append(relativa)
                break
    return infractores


def test_el_filtro_de_razonamiento_tiene_un_solo_dueno() -> None:
    """Ancla que ENUMERA: un segundo filtro (p. ej. por superficie) diverge del del router y deja un hermano sin cubrir."""
    assert _modulos_con_filtro_de_razonamiento_propio() == []


class TestOcultarRazonamiento:
    @pytest.mark.parametrize(
        "texto",
        [
            "",
            "Hola, ¿cómo andás?",
            "  espacios y\n\nsaltos se conservan  ",
            "<b>HTML propio</b> y <i>más</i>",
            "a < b y c > d",
            "<thoughtful>no es una etiqueta de razonamiento</thoughtful>",
        ],
    )
    def test_un_texto_sin_etiquetas_de_razonamiento_queda_identico(self, texto: str) -> None:
        assert ocultar_razonamiento(texto) == texto

    def test_quita_un_bloque_thought_al_inicio(self) -> None:
        assert ocultar_razonamiento("<thought>pienso paso a paso</thought>\nHola") == "Hola"

    def test_quita_un_bloque_think(self) -> None:
        assert ocultar_razonamiento("<think>razono</think>Listo.") == "Listo."

    def test_las_etiquetas_no_distinguen_mayusculas(self) -> None:
        assert ocultar_razonamiento("<THOUGHT>x</Thought>Respuesta") == "Respuesta"

    def test_quita_un_bloque_multilinea(self) -> None:
        texto = "<thought>\nlinea 1\nlinea 2\n</thought>\n\nRespuesta final"
        assert ocultar_razonamiento(texto) == "Respuesta final"

    def test_quita_varios_bloques_intercalados(self) -> None:
        texto = "<thought>a</thought>Uno <think>b</think>dos"
        assert ocultar_razonamiento(texto) == "Uno dos"

    def test_los_bloques_anidados_se_quitan_enteros(self) -> None:
        texto = "<thought>a<thought>b</thought>c</thought>Listo"
        assert ocultar_razonamiento(texto) == "Listo"

    def test_una_etiqueta_de_cierre_huerfana_se_descarta(self) -> None:
        assert ocultar_razonamiento("Hola</thought>") == "Hola"

    def test_una_apertura_sin_cierre_oculta_hasta_el_final(self) -> None:
        # El modelo se cortó razonando: lo ya dicho se conserva, el resto es razonamiento.
        assert ocultar_razonamiento("Hola <thought>razonando sin cerrar") == "Hola"

    def test_si_todo_era_razonamiento_se_conserva_el_contenido_sin_etiquetas(self) -> None:
        # Un mensaje vacío lo rechaza Telegram igual que el HTML inválido: es preferible
        # mostrar el contenido que fabricar un «error interno» con un texto en blanco.
        assert ocultar_razonamiento("<thought>solo esto</thought>") == "solo esto"

    def test_si_todo_era_una_apertura_sin_cierre_se_conserva_el_contenido(self) -> None:
        assert ocultar_razonamiento("<thought>solo esto") == "solo esto"

    def test_un_caracter_que_cambia_de_largo_al_minusculizar_no_corrompe_el_corte(self) -> None:
        # ``"İ".lower()`` mide 2 caracteres: buscar índices en una copia en minúsculas
        # desalinearía el recorte.
        assert ocultar_razonamiento("İ<thought>x</thought>ok") == "İok"

    def test_es_idempotente(self) -> None:
        una_vez = ocultar_razonamiento("<thought>a</thought>Hola <think>b</think>mundo")
        assert ocultar_razonamiento(una_vez) == una_vez

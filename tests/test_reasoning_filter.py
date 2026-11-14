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

from sky_claw.app.agent.reasoning_filter import (
    MENSAJE_SIN_RESPUESTA,
    ocultar_razonamiento,
    quitar_bloques_de_razonamiento,
)

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

    @pytest.mark.parametrize(
        "texto",
        [
            "<thought>Razonamiento interno confidencial</thought>",
            "<think>Razonamiento interno confidencial",
            "  <THINK>Razonamiento interno confidencial</think>  \n",
            "<thought>Razonamiento interno confidencial</thought><think>otro</think>",
        ],
    )
    def test_si_todo_era_razonamiento_se_devuelve_un_aviso_neutro_y_nunca_ese_contenido(self, texto: str) -> None:
        # Un mensaje vacío lo rechaza Telegram igual que el HTML inválido, pero el contenido
        # del razonamiento es justo lo que se prometió ocultar: el filtro falla cerrado.
        resultado = ocultar_razonamiento(texto)

        assert resultado == MENSAJE_SIN_RESPUESTA
        assert "confidencial" not in resultado
        assert "otro" not in resultado

    @pytest.mark.parametrize(
        ("texto", "esperado"),
        [
            # Un cierre de OTRO tipo no cierra el bloque abierto: lo que sigue sigue oculto.
            ("<thought>SECRETO1</think>SECRETO2</thought>Respuesta", "Respuesta"),
            ("<think>SECRETO1</thought>SECRETO2</think>Respuesta", "Respuesta"),
            # Tipos distintos anidados: cada cierre empareja con su apertura.
            ("<thought>a<think>SECRETO</think>c</thought>Respuesta", "Respuesta"),
            ("<think>a<thought>SECRETO</thought>c</think>Respuesta", "Respuesta"),
            ("<thought>a</think></think>SECRETO</thought>Respuesta", "Respuesta"),
        ],
    )
    def test_un_cierre_que_no_corresponde_no_cierra_el_bloque_ni_expone_su_contenido(
        self, texto: str, esperado: str
    ) -> None:
        resultado = ocultar_razonamiento(texto)

        assert resultado == esperado
        assert "SECRETO" not in resultado

    def test_si_el_cierre_que_no_corresponde_deja_todo_oculto_el_resultado_es_el_aviso_neutro(self) -> None:
        resultado = ocultar_razonamiento("<think>SECRETO1</thought>SECRETO2")

        assert resultado == MENSAJE_SIN_RESPUESTA

    def test_las_etiquetas_dentro_de_codigo_markdown_tambien_se_ocultan(self) -> None:
        """Decisión documentada: el filtro NO respeta los backticks.

        Respetarlos abriría un bypass: bastaría envolver el razonamiento en un span de código para
        mostrarlo. El costo es que un ejemplo literal de estas etiquetas en una explicación técnica
        pierde su contenido; el SYSTEM_PROMPT ya reserva ``<thought>`` para el razonamiento.
        """
        resultado = ocultar_razonamiento("Ejemplo: `<think>texto</think>`")

        assert "texto" not in resultado


class TestQuitarBloquesDeRazonamiento:
    """Variante sin aviso: es la que ve el parser de llamadas a herramientas (XML)."""

    def test_devuelve_cadena_vacia_si_todo_era_razonamiento(self) -> None:
        assert quitar_bloques_de_razonamiento("<thought>solo esto</thought>") == ""

    def test_quita_las_llamadas_a_herramientas_escritas_dentro_del_razonamiento(self) -> None:
        texto = '<thought>\n<tool_call>{"name": "tool_x", "arguments": {}}</tool_call>\n</thought>\nListo'

        assert quitar_bloques_de_razonamiento(texto) == "Listo"

    def test_deja_intacto_lo_que_no_es_razonamiento(self) -> None:
        texto = '<tool_call>{"name": "tool_x", "arguments": {}}</tool_call>'

        assert quitar_bloques_de_razonamiento(texto) == texto

    def test_un_caracter_que_cambia_de_largo_al_minusculizar_no_corrompe_el_corte(self) -> None:
        # ``"İ".lower()`` mide 2 caracteres: buscar índices en una copia en minúsculas
        # desalinearía el recorte.
        assert ocultar_razonamiento("İ<thought>x</thought>ok") == "İok"

    def test_es_idempotente(self) -> None:
        una_vez = ocultar_razonamiento("<thought>a</thought>Hola <think>b</think>mundo")
        assert ocultar_razonamiento(una_vez) == una_vez

"""Oculta el razonamiento del modelo (``<thought>``/``<think>``) de lo que ve el usuario.

El ``SYSTEM_PROMPT`` le pide al modelo razonar dentro de un bloque ``<thought>``
«oculto al usuario»; algunos proveedores (DeepSeek) además emiten ``<think>``.
Nada lo ocultaba: el bloque salía tal cual por Telegram —que lo rechaza como HTML
inválido (#918)—, la GUI, la web y el CLI.

Es la ÚNICA pieza que decide qué es razonamiento. Se invoca en la salida de
``LLMRouter.chat`` (el cuello de botella de todas las superficies), después del
guardrail de salida: la puerta de seguridad ve el texto crudo.

No usa regex: es sanitización de texto de presentación (no se extrae ningún dato
estructurado del LLM) y un escaneo lineal sobre las posiciones de ``<`` evita el
desalineo de índices que produce buscar en una copia en minúsculas (``"İ".lower()``
mide dos caracteres).
"""

from __future__ import annotations

# (etiqueta en minúsculas, ¿abre un bloque?). Congelado por los tests: sumar una
# variante es una decisión de producto, no un detalle.
_ETIQUETAS: tuple[tuple[str, bool], ...] = (
    ("<thought>", True),
    ("</thought>", False),
    ("<think>", True),
    ("</think>", False),
)


def _etiqueta_en(texto: str, posicion: int) -> tuple[int, bool] | None:
    """Largo y tipo de la etiqueta de razonamiento que empieza en ``posicion``, si hay una."""
    for etiqueta, abre in _ETIQUETAS:
        largo = len(etiqueta)
        if texto[posicion : posicion + largo].lower() == etiqueta:
            return largo, abre
    return None


def ocultar_razonamiento(texto: str) -> str:
    """Devuelve ``texto`` sin los bloques de razonamiento.

    - Quita cada bloque ``<thought>…</thought>`` / ``<think>…</think>`` (sin distinguir
      mayúsculas, multilínea, anidados y repetidos).
    - Una etiqueta de cierre huérfana se descarta; una apertura sin cierre oculta hasta
      el final (el modelo se cortó razonando).
    - Si todo el mensaje era razonamiento se conserva su contenido sin etiquetas: un
      texto en blanco lo rechaza Telegram igual que el HTML inválido, y mostrar el
      contenido es mejor que fabricar un «error interno».
    - Sin etiquetas de razonamiento devuelve ``texto`` idéntico (el HTML propio u
      ajeno no se toca: eso lo resuelve cada transporte).
    """
    visible: list[str] = []
    contenido: list[str] = []
    profundidad = 0
    hubo_etiqueta = False
    inicio = 0
    cursor = 0

    while True:
        posicion = texto.find("<", cursor)
        if posicion == -1:
            break
        etiqueta = _etiqueta_en(texto, posicion)
        if etiqueta is None:
            cursor = posicion + 1
            continue
        largo, abre = etiqueta
        tramo = texto[inicio:posicion]
        contenido.append(tramo)
        if profundidad == 0:
            visible.append(tramo)
        hubo_etiqueta = True
        if abre:
            profundidad += 1
        elif profundidad > 0:
            profundidad -= 1
        inicio = cursor = posicion + largo

    if not hubo_etiqueta:
        return texto

    cola = texto[inicio:]
    contenido.append(cola)
    if profundidad == 0:
        visible.append(cola)

    resultado = "".join(visible).strip()
    return resultado or "".join(contenido).strip()

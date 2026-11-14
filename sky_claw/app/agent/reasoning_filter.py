"""Oculta el razonamiento del modelo (``<thought>``/``<think>``) de lo que ve el usuario.

El ``SYSTEM_PROMPT`` le pide al modelo razonar dentro de un bloque ``<thought>``
«oculto al usuario»; algunos proveedores (DeepSeek) además emiten ``<think>``.
Nada lo ocultaba: el bloque salía tal cual por Telegram —que lo rechaza como HTML
inválido (#918)—, la GUI, la web y el CLI.

Es la ÚNICA pieza que decide qué es razonamiento. Se invoca en la salida de
``LLMRouter.chat`` (el cuello de botella de todas las superficies), después del
guardrail de salida: la puerta de seguridad ve el texto crudo. El parser de
llamadas a herramientas XML usa :func:`quitar_bloques_de_razonamiento` para no
tratar como orden lo que el modelo escribió mientras «pensaba».

Propiedad de seguridad: **el filtro falla cerrado**. Ante una entrada inesperada
(cierres que no corresponden, bloques sin cerrar, una respuesta que era solo
razonamiento) oculta de más; nunca devuelve el contenido de un bloque abierto.

No usa regex: es sanitización de texto de presentación (no se extrae ningún dato
estructurado del LLM) y un escaneo lineal sobre las posiciones de ``<`` evita el
desalineo de índices que produce buscar en una copia en minúsculas (``"İ".lower()``
mide dos caracteres).

Decisión documentada: las etiquetas dentro de código Markdown (entre backticks) también se ocultan. Respetar los backticks abriría un bypass (bastaría envolver el
razonamiento en un span de código para mostrarlo); el costo es que un ejemplo literal
de estas etiquetas en una explicación técnica pierde su contenido.
"""

from __future__ import annotations

#: Lo que se le muestra al usuario cuando la respuesta era SOLO razonamiento. Un mensaje vacío lo rechaza
#: Telegram igual que el HTML inválido, y devolver el razonamiento sería justo lo que se prometió ocultar.
MENSAJE_SIN_RESPUESTA = "⚠️ El modelo no generó una respuesta final. Intentá nuevamente."

# (etiqueta en minúsculas, tipo de bloque, ¿abre?). Congelado por los tests: sumar una variante es una
# decisión de producto, no un detalle.
_ETIQUETAS: tuple[tuple[str, str, bool], ...] = (
    ("<thought>", "thought", True),
    ("</thought>", "thought", False),
    ("<think>", "think", True),
    ("</think>", "think", False),
)


def _etiqueta_en(texto: str, posicion: int) -> tuple[int, str, bool] | None:
    """Largo, tipo y sentido de la etiqueta de razonamiento que empieza en ``posicion``, si hay una."""
    for etiqueta, tipo, abre in _ETIQUETAS:
        largo = len(etiqueta)
        if texto[posicion : posicion + largo].lower() == etiqueta:
            return largo, tipo, abre
    return None


def _separar(texto: str) -> tuple[str, bool]:
    """Texto visible (fuera de todo bloque de razonamiento) y si había alguna etiqueta de razonamiento.

    Los bloques se llevan en una pila: un cierre solo cierra al bloque abierto de SU tipo. Un cierre de
    otro tipo dentro de un bloque no lo cierra (lo que sigue sigue oculto); fuera de todo bloque, un cierre
    huérfano se descarta. Un bloque que nunca se cierra oculta hasta el final.
    """
    visible: list[str] = []
    pila: list[str] = []
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
        largo, tipo, abre = etiqueta
        if not pila:
            visible.append(texto[inicio:posicion])
        hubo_etiqueta = True
        if abre:
            pila.append(tipo)
        elif pila and pila[-1] == tipo:
            pila.pop()
        inicio = cursor = posicion + largo

    if not pila:
        visible.append(texto[inicio:])
    return "".join(visible), hubo_etiqueta


def quitar_bloques_de_razonamiento(texto: str) -> str:
    """Devuelve ``texto`` sin los bloques de razonamiento, sin aviso de reemplazo.

    Puede devolver una cadena vacía (si todo era razonamiento). Sin etiquetas devuelve ``texto`` idéntico.
    Es la vista que consume el parser de llamadas a herramientas XML: una ``<tool_call>`` escrita dentro
    del razonamiento no es una orden.
    """
    visible, hubo_etiqueta = _separar(texto)
    return visible.strip() if hubo_etiqueta else texto


def ocultar_razonamiento(texto: str) -> str:
    """Devuelve ``texto`` listo para mostrarse: sin bloques de razonamiento.

    - Quita cada bloque ``<thought>…</thought>`` / ``<think>…</think>`` (sin distinguir mayúsculas,
      multilínea, anidados y repetidos), emparejando cada cierre con su apertura.
    - Una etiqueta de cierre huérfana se descarta; una apertura sin cierre oculta hasta el final
      (el modelo se cortó razonando).
    - Si no quedó nada visible devuelve :data:`MENSAJE_SIN_RESPUESTA`, nunca el contenido del razonamiento.
    - Sin etiquetas de razonamiento devuelve ``texto`` idéntico (el HTML propio u ajeno no se toca:
      eso lo resuelve cada transporte).
    """
    visible, hubo_etiqueta = _separar(texto)
    if not hubo_etiqueta:
        return texto
    return visible.strip() or MENSAJE_SIN_RESPUESTA
